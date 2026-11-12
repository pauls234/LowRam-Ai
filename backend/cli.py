"""Command line interface: ``python3 lowram.py <command> ...``."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__, agent, artifacts, config, db, llm, memory, runner, skills

GIB = 1024**3


def _out(args: argparse.Namespace, data: object, text: str) -> None:
    print(json.dumps(data, indent=2, default=str) if args.json else agent.clean_text(text))


def _ram_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--ram", type=int, default=memory.DEFAULT_PERCENT, metavar="PERCENT",
        help="planning budget in percent of total RAM (20-80, scheduling hint, default 40)",
    )


# --------------------------------------------------------------------------- commands


def cmd_memory(args: argparse.Namespace) -> int:
    plan = memory.plan(args.ram)
    text = (
        f"Total RAM:      {plan.total_bytes / GIB:.1f} GiB\n"
        f"Available:      {plan.available_bytes / GIB:.1f} GiB ({100 - plan.used_percent:.0f}% free)\n"
        f"Budget ({plan.percent}%):  {plan.budget_bytes / GIB:.1f} GiB -> context {plan.context_chars} chars, "
        f"max_tokens {plan.max_tokens}\n"
        f"Status:         {'ready' if plan.can_run else 'PAUSED'} - {plan.reason}"
    )
    _out(args, plan.to_dict(), text)
    return 0 if plan.can_run else 3


def cmd_new(args: argparse.Namespace) -> int:
    lines = list(args.task or [])
    if args.file:
        lines.extend(Path(args.file).read_text(encoding="utf-8").splitlines())
    project_id = runner.create_project(args.name, "\n".join(lines))
    project = db.get_project(project_id)
    _out(args, project, f"Created project #{project_id} with {project['total']} task(s).")
    return 0


def cmd_create(args: argparse.Namespace) -> int:
    project_id = agent.create_coding_project(args.goal, args.repo)
    _out(args, {"project_id": project_id}, f"Created coding project #{project_id}. Next: lowram.py plan {project_id}")
    return 0


def cmd_projects(args: argparse.Namespace) -> int:
    projects = db.list_projects()
    rows = [
        f"#{p['id']:<4} {p['kind']:<8} {p['counts']['done'] + p['counts']['approved']}/{p['total']} done  {p['name']}"
        for p in projects
    ]
    _out(args, projects, "\n".join(rows) or "No projects yet.")
    return 0


def cmd_tasks(args: argparse.Namespace) -> int:
    tasks = db.list_tasks(args.project_id)
    rows = []
    for t in tasks:
        rows.append(f"#{t['id']:<5} [{t['status']:<8}] {t['position']:>3}. {t['title']}")
        if t["error"]:
            rows.append(f"        ! {t['error']}")
        if t["result_path"]:
            rows.append(f"        -> {t['result_path']}")
    _out(args, tasks, "\n".join(rows) or "No tasks.")
    return 0


def cmd_plan(args: argparse.Namespace) -> int:
    tasks = agent.plan(args.project_id, args.ram)
    rows = [f"#{t['id']:<5} {t['position']:>3}. {t['title']}" for t in tasks if t["status"] == "pending"]
    _out(args, tasks, "Plan:\n" + "\n".join(rows) + f"\nNext: lowram.py step {args.project_id} --ram {args.ram}")
    return 0


def cmd_add_step(args: argparse.Namespace) -> int:
    task_id = agent.add_step(args.project_id, args.title, args.file or [], args.detail or "")
    _out(args, {"task_id": task_id}, f"Added step #{task_id}.")
    return 0


def _print_proposal(result: dict) -> None:
    clean = agent.clean_text
    print(clean(f"Proposal for task #{result['task_id']}: {result['step']}"))
    print(clean(f"Summary: {result['summary'] or '-'}"))
    for change in result["files"]:
        flag = "  <-- ATTENTION: runs code on build/test/install" if change.get("attention") else ""
        print(f"  {change['action']:<6} {change['path']}{flag}")
    for warning in result.get("warnings", []):
        print(clean(f"WARNING: {warning}"))
    print(f"Proposal: {result['proposal_path']}\nDiff:     {result['diff_path']}")


def cmd_step(args: argparse.Namespace) -> int:
    result = agent.execute_coding_step(args.project_id, args.ram)
    if args.json:
        _out(args, result, "")
    elif result["status"] == "proposed":
        _print_proposal(result)
        print(f"\nInspect the proposal, then: lowram.py approve {args.project_id} {result['task_id']}")
        print(f"                       or: lowram.py reject {args.project_id} {result['task_id']}")
    elif result["status"] == "failed":
        print(agent.clean_text(f"Step #{result['task_id']} failed:\n  - " + "\n  - ".join(result["errors"])))
        print(f"Fix the cause (or adjust the step), then queue it again: lowram.py retry {result['task_id']}")
    else:
        print(f"{result['status']}: {result['reason']}")
    return 0 if result["status"] in ("proposed", "empty") else 1


def cmd_show(args: argparse.Namespace) -> int:
    proposal = agent.load_proposal(args.project_id, args.task_id)
    _out(args, proposal, f"{proposal['step']}\n{proposal['summary']}\n\n{proposal['diff']}")
    return 0


def cmd_approve(args: argparse.Namespace) -> int:
    result = agent.approve(args.project_id, args.task_id)
    text = f"Applied task #{args.task_id}: " + ", ".join(result["written"])
    if result["backup_dir"]:
        text += f"\nBackups of the previous versions: {result['backup_dir']}"
    _out(args, result, text)
    return 0


def cmd_reject(args: argparse.Namespace) -> int:
    task = agent.reject(args.project_id, args.task_id, args.reason or "")
    _out(args, task, f"Rejected task #{args.task_id}; the repository was not changed.")
    return 0


def cmd_loop(args: argparse.Namespace) -> int:
    """Coding loop: propose -> show diff -> ask the human -> apply/reject -> next step."""
    if not 1 <= args.max_steps <= 50:
        print("Error: --max-steps must be between 1 and 50", file=sys.stderr)
        return 2
    if not sys.stdin.isatty():
        print("The coding loop needs an interactive terminal: every change must be approved by a human.",
              file=sys.stderr)
        return 2
    for _ in range(args.max_steps):
        result = agent.execute_coding_step(args.project_id, args.ram)
        if result["status"] != "proposed":
            print(f"Loop stopped - {result['status']}: " + (result.get("reason") or "; ".join(result.get("errors", []))))
            return 0 if result["status"] == "empty" else 1
        _print_proposal(result)
        print("\n" + (result["diff"] or "(no textual diff)") + "\n")
        while True:
            try:
                answer = input("Apply this change? [y]es / [n]o, reject / [q]uit and decide later: ").strip().lower()
            except EOFError:
                answer = "q"
            if answer in ("y", "yes", "n", "no", "q", "quit"):
                break
        if answer.startswith("y"):
            applied = agent.approve(args.project_id, result["task_id"])
            print("Applied: " + ", ".join(applied["written"]))
        elif answer.startswith("n"):
            agent.reject(args.project_id, result["task_id"], "rejected in loop")
            print("Rejected.")
        else:
            print(f"Left task #{result['task_id']} for review.")
            return 0
    print(f"Reached --max-steps {args.max_steps}.")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    def on_step(outcome: dict) -> None:
        task = outcome.get("task") or {}
        detail = outcome.get("error") or outcome.get("reason") or ""
        print(f"[{outcome['status']}] {('#' + str(task['id']) + ' ' + task['title']) if task else ''} {detail}".rstrip())

    summary = runner.run_loop(
        args.project_id, args.ram, args.max_tasks, on_step=None if args.json else on_step,
        stop_on_failure=not args.keep_going,
    )
    _out(args, summary, f"Loop finished ({summary['stopped_because']}): {summary['completed']} done, "
                        f"{summary['failed']} failed. {summary['message']}")
    if summary["failed"]:
        return 1
    return 3 if summary["stopped_because"] in ("busy", "paused_low_ram") else 0


def cmd_result(args: argparse.Namespace) -> int:
    task = db.get_task(args.task_id)
    content = artifacts.read_result(task["result_path"])
    _out(args, {"task_id": args.task_id, "content": content}, content)
    return 0


def cmd_retry(args: argparse.Namespace) -> int:
    task = db.retry_task(args.task_id)
    _out(args, task, f"Task #{args.task_id} is pending again.")
    return 0


def cmd_recover(args: argparse.Namespace) -> int:
    ids = db.recover_stale(force=args.force)
    _out(args, {"recovered": ids}, f"Recovered task(s): {ids}" if ids else "Nothing to recover.")
    return 0


def cmd_skill_install(args: argparse.Namespace) -> int:
    meta = skills.install_skill(args.url, args.name, overwrite=args.overwrite)
    _out(args, meta, f"Stored {meta['path']} ({meta['bytes']} bytes, sha256 {meta['sha256']}).\n"
                     "The skill is NOT trusted and NOT loaded into prompts. Review it yourself.")
    return 0


def cmd_skill_list(args: argparse.Namespace) -> int:
    items = skills.list_skills()
    rows = [
        f"{s['name']:<24} {s['error'] if s.get('error') else ('ok' if s['checksum_ok'] else 'CHECKSUM MISMATCH')}"
        f"  {s.get('url', '')}"
        for s in items
    ]
    _out(args, items, "\n".join(rows) or "No skills installed.")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    from . import app

    return app.main([])


def cmd_app(args: argparse.Namespace) -> int:
    """Start the desktop app (native window if pywebview is installed, else the browser)."""
    from desktop import launcher

    return launcher.main(["--no-window"] if args.browser else [])


def cmd_worker(args: argparse.Namespace) -> int:
    """Run this machine as a helper device for another LowRAM AI."""
    from . import worker

    return worker.run_cli(args)


# --------------------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="lowram.py", description="LowRAM AI - local, low-memory task runner")
    parser.add_argument("--version", action="version", version=f"LowRAM AI {__version__}")
    parser.add_argument("--json", action="store_true", help="print machine-readable JSON")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("memory", help="show RAM and the planning budget")
    _ram_arg(p)
    p.set_defaults(func=cmd_memory)

    p = sub.add_parser("serve", help="start the local dashboard on 127.0.0.1:8765")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("app", help="start LowRAM AI Studio (desktop window)")
    p.add_argument("--browser", action="store_true", help="open in the default browser instead of a window")
    p.set_defaults(func=cmd_app)

    p = sub.add_parser("worker", help="help another LowRAM AI on your network as a worker device")
    p.add_argument("--join", metavar="PAIRING_CODE", required=True, help="code shown on the host under Devices")
    p.add_argument("--name", help="name of this device as shown on the host")
    _ram_arg(p)
    p.set_defaults(func=cmd_worker)

    p = sub.add_parser("new", help="create a general project (one task per --task or line in --file)")
    p.add_argument("name")
    p.add_argument("--task", action="append", help="a task (repeatable)")
    p.add_argument("--file", help="text file with one task per line")
    p.set_defaults(func=cmd_new)

    p = sub.add_parser("run", help="run pending tasks of a general project in a bounded loop")
    p.add_argument("project_id", type=int)
    _ram_arg(p)
    p.add_argument("--max-tasks", type=int, default=runner.DEFAULT_LOOP_TASKS, help="stop after N tasks (1-100)")
    p.add_argument("--keep-going", action="store_true", help="continue after a failed task")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("projects", help="list projects")
    p.set_defaults(func=cmd_projects)

    p = sub.add_parser("tasks", help="list the tasks of a project")
    p.add_argument("project_id", type=int)
    p.set_defaults(func=cmd_tasks)

    p = sub.add_parser("result", help="print the stored result of a task")
    p.add_argument("task_id", type=int)
    p.set_defaults(func=cmd_result)

    p = sub.add_parser("retry", help="put a failed task back into the queue")
    p.add_argument("task_id", type=int)
    p.set_defaults(func=cmd_retry)

    p = sub.add_parser("recover", help="mark tasks left 'running' by a crashed process as failed")
    p.add_argument("--force", action="store_true", help="also recover tasks whose owner looks alive")
    p.set_defaults(func=cmd_recover)

    p = sub.add_parser("create", help="create a coding project for a local repository")
    p.add_argument("repo", help="absolute path to the repository")
    p.add_argument("goal")
    p.set_defaults(func=cmd_create)

    p = sub.add_parser("plan", help="ask the model for a small plan")
    p.add_argument("project_id", type=int)
    _ram_arg(p)
    p.set_defaults(func=cmd_plan)

    p = sub.add_parser("add-step", help="add a hand-written step to a coding project")
    p.add_argument("project_id", type=int)
    p.add_argument("title")
    p.add_argument("--file", action="append", help="relative path the step may change (repeatable)")
    p.add_argument("--detail", help="what to change and why")
    p.set_defaults(func=cmd_add_step)

    p = sub.add_parser("step", help="generate a proposal for the next step (no files are changed)")
    p.add_argument("project_id", type=int)
    _ram_arg(p)
    p.set_defaults(func=cmd_step)

    p = sub.add_parser("show", help="print a proposal's summary and diff")
    p.add_argument("project_id", type=int)
    p.add_argument("task_id", type=int)
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("approve", help="apply a reviewed proposal to the repository")
    p.add_argument("project_id", type=int)
    p.add_argument("task_id", type=int)
    p.set_defaults(func=cmd_approve)

    p = sub.add_parser("reject", help="discard a proposal")
    p.add_argument("project_id", type=int)
    p.add_argument("task_id", type=int)
    p.add_argument("--reason")
    p.set_defaults(func=cmd_reject)

    p = sub.add_parser("loop", help="interactive coding loop: propose, review, approve/reject, repeat")
    p.add_argument("project_id", type=int)
    _ram_arg(p)
    p.add_argument("--max-steps", type=int, default=5)
    p.set_defaults(func=cmd_loop)

    p = sub.add_parser("skill", help="manage downloaded (untrusted) Markdown skills")
    skill_sub = p.add_subparsers(dest="skill_command", required=True)
    s = skill_sub.add_parser("install", help="download a skill from raw.githubusercontent.com")
    s.add_argument("url")
    s.add_argument("name")
    s.add_argument("--overwrite", action="store_true")
    s.set_defaults(func=cmd_skill_install)
    s = skill_sub.add_parser("list", help="list installed skills and verify checksums")
    s.set_defaults(func=cmd_skill_list)
    return parser


EXPECTED_ERRORS = (
    ValueError, LookupError, db.Busy, llm.LLMError, agent.AgentError, skills.SkillError,
    artifacts.ArtifactError, config.ConfigError, OSError,
)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
    except EXPECTED_ERRORS as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
