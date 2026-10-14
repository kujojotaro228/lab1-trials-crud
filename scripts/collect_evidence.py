#!/usr/bin/env python3

from __future__ import annotations

from _util import REPO_ROOT, RESULTS_DIR, compose, image_name, md_block, read_env, sh


def main() -> None:
    env = read_env()
    user = env.get("POSTGRES_USER", "trials")
    dbname = env.get("POSTGRES_DB", "trials")
    password = env.get("POSTGRES_PASSWORD", "")
    img = image_name()
    out = ["# Докази виконання вимог", ""]

    def cmd(title, args, shown=None):
        r = sh(args)
        text = (r.stdout + ("\n" + r.stderr if r.stderr.strip() else "")).rstrip()
        out.append(md_block(title, shown or " ".join(args), text))
        print(f"collected: {title}")
        return r

    out.append("## Середовище\n")
    cmd("Версія Docker", ["docker", "version", "--format",
                          "Client {{.Client.Version}} / Server {{.Server.Version}}"])
    cmd("Версія Compose", ["docker", "compose", "version"])
    cmd("Стан стенда", ["docker", "compose", "ps", "-a"])

    out.append("## Додаткова вимога: схема накочується окремим разовим контейнером\n")
    cmd("Контейнер міграції завершився з кодом 0",
        ["docker", "compose", "ps", "-a", "migrate"])
    cmd("Логи контейнера міграції", ["docker", "compose", "logs", "--no-log-prefix", "migrate"])
    cmd("Тека ініціалізації бази порожня",
        ["docker", "compose", "exec", "-T", "db", "ls", "-la", "/docker-entrypoint-initdb.d"])
    db_id = compose("ps", "-q", "db").stdout.strip()
    if db_id:
        cmd("Монтування контейнера бази (лише том з даними)",
            ["docker", "inspect", "--format",
             "{{range .Mounts}}{{.Type}} {{.Name}} -> {{.Destination}}\n{{end}}", db_id],
            "docker inspect --format '{{range .Mounts}}...{{end}}' <db>")
    cmd("Журнал застосованих міграцій",
        ["docker", "compose", "exec", "-T", "db", "psql", "-U", user, "-d", dbname, "-c",
         "SELECT version, left(checksum, 12) AS checksum, applied_at FROM schema_migrations"])
    cmd("Структура таблиці",
        ["docker", "compose", "exec", "-T", "db", "psql", "-U", user, "-d", dbname, "-c",
         r"\d trial_participants"])
    cmd("Повторний запуск міграції — ідемпотентний (нічого не застосовує)",
        ["docker", "compose", "run", "--rm", "--no-deps", "migrate"])

    out.append("## Ліміти ресурсів вебінстансу\n")
    web_ids = [x for x in compose("ps", "-q", "web").stdout.split() if x]
    for cid in web_ids:
        cmd(f"Ліміти контейнера {cid[:12]}",
            ["docker", "inspect", "--format",
             "{{.Name}} NanoCpus={{.HostConfig.NanoCpus}} Memory={{.HostConfig.Memory}}", cid],
            f"docker inspect --format '{{{{.Name}}}} NanoCpus=... Memory=...' {cid[:12]}")
    if web_ids:
        cmd("docker stats (миттєвий знімок)",
            ["docker", "stats", "--no-stream", "--format",
             "table {{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}", *web_ids])
    out.append("NanoCpus=500000000 означає 0.5 CPU; Memory=536870912 — 512 MiB.\n")

    out.append("## Образ: багатоетапна збірка і відсутність пароля\n")
    cmd("Розмір образу", ["docker", "image", "ls", img])
    cmd("Шари образу", ["docker", "history", img])
    r = sh(["docker", "run", "--rm", "--entrypoint", "sh", img, "-c",
            "command -v gcc || echo 'gcc: немає'; command -v cc || echo 'cc: немає'; "
            "id; python --version"])
    out.append(md_block("У фінальній стадії немає компілятора; процес не від root",
                        f"docker run --rm --entrypoint sh {img} -c 'command -v gcc; id; python --version'",
                        r.stdout + r.stderr))

    hist = sh(["docker", "history", "--no-trunc", img]).stdout
    env_cfg = sh(["docker", "image", "inspect", img, "--format", "{{json .Config.Env}}"]).stdout
    compose_txt = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    docker_txt = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
    checks = []
    if password:
        checks += [
            ("пароль з .env відсутній в історії збірки (docker history --no-trunc)", password not in hist),
            ("пароль з .env відсутній у змінних середовища образу", password not in env_cfg),
            ("пароль з .env відсутній у docker-compose.yml", password not in compose_txt),
            ("пароль з .env відсутній у Dockerfile", password not in docker_txt),
        ]
    checks.append(("у змінних середовища образу немає DB_PASSWORD / POSTGRES_PASSWORD",
                   "PASSWORD" not in env_cfg))
    git_ls = sh(["git", "ls-files", ".env"])
    if git_ls.returncode == 0:
        checks.append(("файл .env не відстежується git", git_ls.stdout.strip() == ""))
    out.append("### Перевірки пароля\n")
    for title, ok in checks:
        out.append(f"- {'✅' if ok else '❌'} {title}")
    out.append("")
    out.append(md_block("Змінні середовища образу", f"docker image inspect {img} --format '{{{{json .Config.Env}}}}'", env_cfg))

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    (RESULTS_DIR / "evidence.md").write_text("\n".join(out) + "\n", encoding="utf-8")
    print("saved results/evidence.md")


if __name__ == "__main__":
    main()
