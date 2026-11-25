#!/usr/bin/env python3
"""Lab 6 training script: one process (no torch.distributed) or P processes via torchrun.

Modes (--mode):
  train     n_ep epochs, loss of every step, ACC and AUC on the test split
            (with --ckpt-every / --fail-step: checkpoints, restore at start, simulated failure)
  speed     10 warm-up + 50 measured steps: t_step (median of rank 0), X = 50 * B / t
            (with --nosync: backward inside model.no_sync(), DDP does no all-reduce)
  gradcheck one batch of B examples: g of one process vs averaged g of P processes
  smoke     environment check: gloo all_reduce, all_gather and 2 DDP steps

--sync: none (plain single process, no torch.distributed), manual (all_reduce per tensor),
ddp (DistributedDataParallel). The number of threads per process is LAB6_NTH (n_th).
Results: rank 0 writes --out (JSON); --events gets JSON lines with timestamps (time.time()).
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402

NTH = common.nth_from_env()

import torch  # noqa: E402

torch.set_num_threads(NTH)

import torch.distributed as dist  # noqa: E402
import torch.nn.functional as F  # noqa: E402
from torch.nn.parallel import DistributedDataParallel as DDP  # noqa: E402
from torch.utils.data import DataLoader, DistributedSampler  # noqa: E402

pc = time.perf_counter


def init_group(restart: int) -> None:
    """dist.init_process_group(backend="gloo") with an explicitly created TCPStore.

    Same as the env:// rendezvous that init_process_group() does by itself (MASTER_ADDR,
    MASTER_PORT, RANK, WORLD_SIZE from torchrun; with the torchrun agent store the workers are
    its clients and use the prefix /worker/attempt_<restart>), but with use_libuv=False:
    PyTorch for Windows is built without libuv, and the env:// path ignores USE_LIBUV=0."""
    from datetime import timedelta
    addr, port = os.environ["MASTER_ADDR"], int(os.environ["MASTER_PORT"])
    rank, ws = int(os.environ["RANK"]), int(os.environ["WORLD_SIZE"])
    agent_store = os.environ.get("TORCHELASTIC_USE_AGENT_STORE", "").lower() == "true"
    store = dist.TCPStore(addr, port, ws, is_master=(not agent_store and rank == 0),
                          timeout=timedelta(minutes=30), use_libuv=False)
    if agent_store:
        store = dist.PrefixStore(f"/worker/attempt_{restart}", store)
    dist.init_process_group(backend="gloo", store=store, rank=rank, world_size=ws)


class Ctx:
    def __init__(self, sync: str):
        self.sync = sync
        self.dist = sync != "none"
        self.restart = int(os.environ.get("TORCHELASTIC_RESTART_COUNT", "0"))
        if self.dist:
            init_group(self.restart)
            self.rank, self.P = dist.get_rank(), dist.get_world_size()
        else:
            self.rank, self.P = 0, 1
        self.restart = int(os.environ.get("TORCHELASTIC_RESTART_COUNT", "0"))

    def barrier(self):
        if self.dist:
            dist.barrier()

    def close(self):
        if self.dist:
            dist.barrier()
            dist.destroy_process_group()


class Events:
    """JSON lines with wall-clock timestamps; one file per rank: <events>.r<rank>.jsonl."""

    def __init__(self, path: str | None, rank: int, restart: int):
        self.path = f"{path}.r{rank}.jsonl" if path else None
        self.rank, self.restart = rank, restart

    def __call__(self, event: str, **kw):
        if not self.path:
            return
        rec = {"t": time.time(), "rank": self.rank, "restart": self.restart, "event": event, **kw}
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
            f.flush()
            os.fsync(f.fileno())


def build(ctx: Ctx, args):
    name, K, C, c1, c2, h = common.dataset_info()
    train = common.load_split(args.data, "train", n=common.N_TRAIN)
    test = common.load_split(args.data, "test")
    torch.manual_seed(0)
    model = common.make_net(C, c1, c2, h, K)
    assert sum(p.numel() for p in model.parameters()) == common.n_params_formula(C, c1, c2, h, K)
    return train, test, model, K


def make_loader(train, ctx: Ctx, b: int):
    sampler = DistributedSampler(train, num_replicas=ctx.P, rank=ctx.rank, shuffle=True, seed=0)
    loader = DataLoader(train, batch_size=b, sampler=sampler, drop_last=True, num_workers=0)
    return sampler, loader


def wrap(model, ctx: Ctx):
    if ctx.sync == "ddp":
        return DDP(model)                      # broadcasts parameters and buffers from rank 0
    if ctx.sync == "manual":
        for p in model.parameters():
            dist.broadcast(p.data, src=0)
    return model


def manual_sync(model, P: int) -> float:
    """all_reduce of every gradient tensor, then / P. Returns t_sync (s): first call -> last return."""
    t0 = pc()
    for p in model.parameters():
        dist.all_reduce(p.grad, op=dist.ReduceOp.SUM)
        p.grad /= P
    return pc() - t0


def train_step(model, opt, xb, yb, ctx: Ctx, nosync: bool = False):
    opt.zero_grad(set_to_none=True)
    t_sync = 0.0
    if ctx.sync == "ddp" and nosync:
        # DDP decides in forward() whether backward will all-reduce, so forward must be
        # inside no_sync() too, not only backward
        with model.no_sync():
            loss = F.cross_entropy(model(xb), yb)
            loss.backward()
    else:
        loss = F.cross_entropy(model(xb), yb)
        loss.backward()
    if ctx.sync == "manual":
        t_sync = manual_sync(model, ctx.P)
    opt.step()
    return loss.detach(), t_sync


def global_loss(loss, ctx: Ctx) -> float:
    if not ctx.dist:
        return float(loss)
    t = loss.clone()
    dist.all_reduce(t, op=dist.ReduceOp.SUM)
    return float(t) / ctx.P


def weight_sums(model, ctx: Ctx) -> list[float]:
    s = torch.zeros(1, dtype=torch.float64)
    with torch.no_grad():
        for p in model.parameters():
            s += p.detach().double().sum()
    if not ctx.dist:
        return [float(s)]
    out = [torch.zeros(1, dtype=torch.float64) for _ in range(ctx.P)]
    dist.all_gather(out, s)
    return [float(x) for x in out]


def evaluate(model, test, K: int) -> dict:
    import numpy as np
    from sklearn.metrics import roc_auc_score
    m = model.module if isinstance(model, DDP) else model
    m.eval()
    probs, ys = [], []
    with torch.no_grad():
        for xb, yb in DataLoader(test, batch_size=512, shuffle=False):
            probs.append(torch.softmax(m(xb), dim=1).numpy())
            ys.append(yb.numpy())
    m.train()
    prob = np.concatenate(probs)
    y = np.concatenate(ys)
    acc = float((prob.argmax(1) == y).mean())
    auc = float(roc_auc_score(y, prob, multi_class="ovr", average="macro", labels=list(range(K))))
    return {"acc": acc, "auc": auc, "n_test": int(len(y))}


def base_info(ctx: Ctx, args, b: int) -> dict:
    return {"mode": args.mode, "sync": args.sync, "nosync": args.nosync, "P": ctx.P, "B": args.B,
            "b": b, "lr": args.lr, "n_th": NTH, "torch_threads": torch.get_num_threads(),
            "restart_count": ctx.restart, "tag": args.tag}


# ---------------------------------------------------------------------------- speed
def run_speed(ctx: Ctx, args) -> dict:
    train, test, model, K = build(ctx, args)
    b = args.B // ctx.P
    model = wrap(model, ctx)
    opt = torch.optim.SGD(model.parameters(), lr=args.lr, momentum=common.MOMENTUM)
    sampler, loader = make_loader(train, ctx, b)
    total = common.WARMUP_STEPS + common.MEASURE_STEPS
    step_times, sync_times, wsums = [], [], None
    step = epoch = 0
    t_measure = 0.0
    ctx.barrier()
    while step < total:
        sampler.set_epoch(epoch)
        it = iter(loader)
        while step < total:
            t0 = pc()
            try:
                xb, yb = next(it)
            except StopIteration:
                break
            _, t_sync = train_step(model, opt, xb, yb, ctx, nosync=args.nosync)
            dt = pc() - t0
            step += 1
            if step > common.WARMUP_STEPS:
                step_times.append(dt)
                sync_times.append(t_sync)
                t_measure += dt
            if step == 50 and ctx.dist:
                wsums = weight_sums(model, ctx)          # outside the timed interval
        epoch += 1
    res = base_info(ctx, args, b)
    res.update({
        "steps_measured": len(step_times), "t_measure_s": t_measure,
        "t_step_ms": statistics.median(step_times) * 1000,
        "t_step_mean_ms": statistics.fmean(step_times) * 1000,
        "X": common.MEASURE_STEPS * args.B / t_measure,
        "t_sync_ms": statistics.median(sync_times) * 1000 if ctx.sync == "manual" else None,
        "t_sync_mean_ms": statistics.fmean(sync_times) * 1000 if ctx.sync == "manual" else None,
        "step_times_ms": [t * 1000 for t in step_times],
        "weight_sums_step50": wsums,
        "weight_sum_spread": (max(wsums) - min(wsums)) if wsums else None,
    })
    return res


# ---------------------------------------------------------------------------- gradcheck
def run_gradcheck(ctx: Ctx, args) -> dict:
    train, test, model, K = build(ctx, args)
    model = wrap(model, ctx)                      # manual: broadcast from rank 0
    B = args.B
    xs, ys = train.tensors[0][:B], train.tensors[1][:B]   # first batch of the training split
    res = base_info(ctx, args, B // ctx.P)
    g_full = None
    if ctx.rank == 0:
        model.zero_grad(set_to_none=True)
        F.cross_entropy(model(xs), ys).backward()
        g_full = [p.grad.detach().clone() for p in model.parameters()]
    model.zero_grad(set_to_none=True)
    idx = torch.arange(ctx.rank, B, ctx.P)        # examples r, r + P, r + 2P, ... of the batch
    F.cross_entropy(model(xs[idx]), ys[idx]).backward()
    t_sync = manual_sync(model, ctx.P)
    if ctx.rank == 0:
        per = []
        for (name, p), g in zip(model.named_parameters(), g_full):
            d = (g - p.grad).abs().max().item()
            m = g.abs().max().item()
            per.append({"tensor": name, "shape": list(p.shape), "max_abs_diff": d, "max_abs_g": m,
                        "ratio": d / m if m else None})
        res.update({"per_tensor": per, "max_ratio": max(x["ratio"] for x in per if x["ratio"] is not None),
                    "b_per_process": len(idx), "t_sync_ms": t_sync * 1000})
    return res


# ---------------------------------------------------------------------------- train
def run_train(ctx: Ctx, args) -> dict:
    ev = Events(args.events, ctx.rank, ctx.restart)
    if ctx.rank == 0:
        ev("start", P=ctx.P)
    train, test, model, K = build(ctx, args)
    b = args.B // ctx.P
    opt_params_model = model
    start_epoch = start_skip = step = 0
    ckpt = Path(args.ckpt_dir) / "ckpt.pt" if args.ckpt_dir else None
    state = None
    if ckpt is not None and ckpt.exists():
        state = torch.load(ckpt, map_location="cpu", weights_only=False)
        model.load_state_dict(state["model"])
    model = wrap(model, ctx)
    opt = torch.optim.SGD(model.parameters(), lr=args.lr, momentum=common.MOMENTUM)
    if state is not None:
        opt.load_state_dict(state["opt"])
        start_epoch, start_skip, step = state["epoch"], state["step_in_epoch"], state["step"]
        if ctx.rank == 0:
            ev("restored", step=step, epoch=start_epoch, step_in_epoch=start_skip)
    del opt_params_model
    sampler, loader = make_loader(train, ctx, b)
    n_e = len(loader)
    losses = []                          # (step, epoch, loss) of this process lifetime
    first_after_restore = state is not None
    ctx.barrier()
    t_train0 = time.time()
    for epoch in range(start_epoch, args.epochs):
        sampler.set_epoch(epoch)
        for i, (xb, yb) in enumerate(loader):
            if epoch == start_epoch and i < start_skip:
                continue                  # batches already done before the checkpoint
            if (args.fail_step and step + 1 == args.fail_step and ctx.rank == ctx.P - 1
                    and args.fail_marker and not Path(args.fail_marker).exists()):
                Path(args.fail_marker).write_text(json.dumps({"t": time.time(), "step": step + 1}))
                ev("fail", step=step + 1)
                os._exit(1)               # no checkpoint, no cleanup
            loss, _ = train_step(model, opt, xb, yb, ctx)
            gl = global_loss(loss, ctx)
            step += 1
            losses.append({"step": step, "epoch": epoch, "loss": gl})
            if ctx.rank == 0:
                ev("step", step=step, epoch=epoch, loss=gl)
                if first_after_restore:
                    ev("first_step_after_restore", step=step)
                    first_after_restore = False
            if args.ckpt_every and step % args.ckpt_every == 0:
                if ctx.rank == 0:
                    m = model.module if isinstance(model, DDP) else model
                    st = {"model": m.state_dict(), "opt": opt.state_dict(), "epoch": epoch,
                          "step_in_epoch": i + 1, "step": step}
                    tmp = ckpt.with_suffix(".tmp")
                    torch.save(st, tmp)
                    os.replace(tmp, ckpt)
                    ev("ckpt", step=step, epoch=epoch, step_in_epoch=i + 1)
                ctx.barrier()
    t_train = time.time() - t_train0
    res = base_info(ctx, args, b)
    res.update({"n_e": n_e, "steps_total": step, "losses": losses, "train_time_s": t_train,
                "resumed_from_step": state["step"] if state is not None else None})
    if ctx.rank == 0:
        res.update(evaluate(model, test, K))
        last_ep = [x["loss"] for x in losses if x["epoch"] == args.epochs - 1]
        res["max_loss_last_epoch"] = max(last_ep) if last_ep else None
        ev("done", step=step, acc=res["acc"], auc=res["auc"])
    return res


# ---------------------------------------------------------------------------- smoke
def run_smoke(ctx: Ctx, args) -> dict:
    t = torch.tensor([float(ctx.rank + 1)])
    dist.all_reduce(t)
    g = [torch.zeros(1) for _ in range(ctx.P)]
    dist.all_gather(g, torch.tensor([float(ctx.rank)]))
    train, test, model, K = build(ctx, args)
    model = wrap(model, ctx)
    opt = torch.optim.SGD(model.parameters(), lr=args.lr, momentum=common.MOMENTUM)
    sampler, loader = make_loader(train, ctx, args.B // ctx.P)
    sampler.set_epoch(0)
    it = iter(loader)
    losses = [global_loss(train_step(model, opt, *next(it), ctx)[0], ctx) for _ in range(2)]
    ws = weight_sums(model, ctx)
    return {"mode": "smoke", "P": ctx.P, "all_reduce": float(t), "all_gather": [float(x) for x in g],
            "losses": losses, "weight_sums": ws, "n_th": NTH, "torch": torch.__version__,
            "gloo": dist.is_gloo_available()}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mode", choices=["train", "speed", "gradcheck", "smoke"], required=True)
    ap.add_argument("--sync", choices=["none", "manual", "ddp"], required=True)
    ap.add_argument("--nosync", action="store_true")
    ap.add_argument("--B", type=int, default=common.VARIANT["B"])
    ap.add_argument("--lr", type=float, default=common.LR0)
    ap.add_argument("--epochs", type=int, default=common.N_EPOCHS)
    ap.add_argument("--ckpt-every", type=int, default=0)
    ap.add_argument("--ckpt-dir")
    ap.add_argument("--fail-step", type=int, default=0)
    ap.add_argument("--fail-marker")
    ap.add_argument("--events")
    ap.add_argument("--data", default=str(common.data_path()))
    ap.add_argument("--out")
    ap.add_argument("--tag", default="")
    args = ap.parse_args()
    ctx = Ctx(args.sync)
    if args.B % ctx.P:
        raise SystemExit(f"B = {args.B} is not divisible by P = {ctx.P}")
    fn = {"train": run_train, "speed": run_speed, "gradcheck": run_gradcheck, "smoke": run_smoke}[args.mode]
    res = fn(ctx, args)
    if ctx.rank == 0 and args.out:
        Path(args.out).write_text(json.dumps(res, indent=1), encoding="utf-8")
        short = {k: v for k, v in res.items() if k not in ("losses", "step_times_ms", "per_tensor")}
        print(json.dumps(short, ensure_ascii=False))
    ctx.close()


if __name__ == "__main__":
    main()
