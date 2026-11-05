"""Standalone CLI: python -m trade.qvix or ashare-panic."""
import argparse
import json
import sys
import webbrowser
from pathlib import Path

from .config import Settings
from .calibration import check_calibration
from .report import build_report
from .service import snapshot, sync, backfill_damage
from .store import dumps
from .download import download_market, atomic_json
from .locking import writer_lock


def execute(settings, args):
    if args.command == "report":
        if args.refresh_rps:
            from .rps import refresh_counts
            print(dumps(refresh_counts(settings)), file=sys.stderr)
        path = build_report(settings, start=args.start, end=args.end, normalize=args.normalize)
        print(path)
        if args.open:
            webbrowser.open(path.resolve().as_uri())
        return 0
    if args.command in ("init-data", "update", "download"):
        download = download_market(settings, start=args.start, end=args.end,
                                   attempts=args.retries, refresh=args.command == "update" and args.start is not None,
                                   symbols=getattr(args, "symbols", None))
        if args.command == "download" or not download["complete"]:
            print(dumps(download))
            return 0 if download["complete"] else 2
        print("股票日线和复权因子就绪，开始同步指数、QVIX并计算评分…", file=sys.stderr, flush=True)
        result = sync(settings, start=download.get("recompute_start"), end=download["target_end"])
        result["download"] = download
        if (not any(k in result["errors"] for k in ("panic", "damage", *result.get("index_latest", {})))
                and result.get("panic", {}).get("date") == download["target_end"]):
            path = settings.data_dir / "market/checkpoint.json"
            state = json.loads(path.read_text())
            state.pop("recompute_start", None)
            atomic_json(path, state)
    elif args.command == "damage-backfill":
        result = backfill_damage(settings, start=args.start, end=args.end)
    elif args.command == "calibration-check":
        result = check_calibration(settings, offline=args.offline)
    elif args.command == "sync":
        result = sync(settings, start=args.start, end=args.end, offline=args.offline, qvix_csv=args.qvix_csv)
    else:
        result = snapshot(settings, panic_only=args.panic_only)
    print(dumps(result))
    if not args.no_report:
        try:
            print(f"Report: {build_report(settings)}", file=sys.stderr)
        except ValueError as exc:
            print(f"Report unavailable: {exc}", file=sys.stderr)
    return 2 if result["errors"] else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="A股恐慌 / QVIX：独立下载、计算和本地看板")
    parser.add_argument("--data-dir", type=Path, default=Settings.data_dir, help="全部运行数据目录，默认当前目录下data")
    parser.add_argument("--local-daily", type=Path, default=None, help="可选自定义日线目录")
    parser.add_argument("--actions-db", type=Path, default=None, help="仅兼容外部Hikyuu数据；独立下载版无需此文件")
    parser.add_argument("--rps-counts", type=Path, default=None, help="可选同口径RPS汇总CSV")
    parser.add_argument("--rps-history", type=Path, default=None, help="可选外部RPS排名历史目录")
    parser.add_argument("--window", type=int, default=756)
    parser.add_argument("--min-history", type=int, default=252)
    parser.add_argument("--max-age-seconds", type=int, default=300)
    parser.add_argument("--timeout", type=float, default=20)
    parser.add_argument("--risk-free-rate", type=float, default=.015)
    commands = parser.add_subparsers(dest="command", required=True)
    for name, description in (("init-data", "首次下载股票历史并生成看板，可中断后续传"),
                              ("update", "补齐股票日线、更新复权因子并重算指标"),
                              ("download", "只下载输入；可指定少量股票检查网络")):
        command = commands.add_parser(name, help=description)
        command.add_argument("--start", help="首次默认2009-01-01；更新时指定可回查更早历史")
        command.add_argument("--end", help="截止日期；盘前/盘中自动排除未发布的当日数据")
        command.add_argument("--retries", type=int, default=3)
        command.add_argument("--no-report", action="store_true")
        if name == "download":
            command.add_argument("--symbols", nargs="+", help="诊断样本，如600000.SH 688001.SH 300308.SZ；不会计算市场评分")
    snap = commands.add_parser("snapshot", help="获取盘中快照，不写日线")
    snap.add_argument("--no-report", action="store_true")
    snap.add_argument("--panic-only", action="store_true", help="跳过期权请求，只更新市场压力")
    daily = commands.add_parser("sync", help="从已有股票输入计算指标；不下载股票日线")
    daily.add_argument("--start")
    daily.add_argument("--end")
    daily.add_argument("--offline", action="store_true")
    daily.add_argument("--qvix-csv", type=Path)
    daily.add_argument("--no-report", action="store_true")
    report = commands.add_parser("report", help="用已有指标生成离线交互HTML")
    report.add_argument("--start")
    report.add_argument("--end")
    report.add_argument("--normalize", action="store_true")
    report.add_argument("--open", action="store_true")
    report.add_argument("--refresh-rps", action="store_true", help="可选：汇总用户自备RPS历史CSV")
    damage = commands.add_parser("damage-backfill", help="仅重算长期损伤历史")
    damage.add_argument("--start")
    damage.add_argument("--end")
    damage.add_argument("--no-report", action="store_true")
    calibration = commands.add_parser("calibration-check", help="检查QVIX与自算IV30同日配对")
    calibration.add_argument("--offline", action="store_true")
    calibration.add_argument("--no-report", action="store_true")
    args = parser.parse_args(argv)
    fields = ("data_dir", "local_daily", "actions_db", "window", "min_history", "max_age_seconds", "timeout", "risk_free_rate", "rps_counts", "rps_history")
    try:
        settings = Settings(**{name: getattr(args, name) for name in fields})
        with writer_lock(settings.data_dir):
            return execute(settings, args)
    except KeyboardInterrupt:
        print("已中断。完成的股票下载仍保留，重新运行同一命令可续传。", file=sys.stderr)
        return 130
    except (ValueError, OSError, ImportError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
