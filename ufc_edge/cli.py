"""Operator command line.

Manual-authorisation actions (setting a bankroll, restarting after
exhaustion, releasing the kill switch) are only available here, and they
prompt a human to type an exact confirmation phrase. The prediction/agent
code never imports these commands.

    python -m ufc_edge demo
    python -m ufc_edge init --bankroll 40           (paper mode by default)
    python -m ufc_edge status
    python -m ufc_edge kill --reason "something looks wrong"
    python -m ufc_edge release-kill
    python -m ufc_edge new-bankroll --deposit 40
    python -m ufc_edge verify
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from .config import RiskConfig
from .errors import UfcEdgeError

from .ledger import BankrollService, ManualAuthorisation, bankroll_phrase
from .money import fmt, pounds_to_pence
from .risk import BetProposal
from .timeutil import utcnow

DATA_DIR = Path("data")


def _service(mode: str) -> BankrollService:
    DATA_DIR.mkdir(exist_ok=True)
    return BankrollService(DATA_DIR / f"{mode}.db", mode=mode)


def _ask_authorisation(expected: str) -> ManualAuthorisation:
    print(f"\nThis action needs manual authorisation.\nType exactly:  {expected}")
    confirmation = input("> ").strip()
    operator = input("Your name: ").strip()
    reason = input("Reason: ").strip()
    return ManualAuthorisation(operator, reason, confirmation)


def _print_status(svc: BankrollService) -> None:
    s = svc.status()
    print(f"\n  [{s['mode'].upper()}]  {s['banner']}")
    print(f"  Bankroll cap (authorised capital): {fmt(s['bankroll_cap_pence'])}")
    print(f"  Active betting bankroll:           {fmt(s['active_pence'])}")
    print(f"  Open stakes (unsettled):           {fmt(s['open_stakes_pence'])}")
    print(f"  Protected realised profit:         {fmt(s['protected_pence'])}")
    print(f"  Withdrawn (confirmed transfers):   {fmt(s['withdrawn_pence'])}")
    print(f"  Your total contributions:          {fmt(s['owner_contributed_pence'])}")
    print(f"  Net betting profit/loss:           {fmt(s['net_pnl_pence'])}")
    print(f"  Note: {s['protected_funds_note']}\n")


def cmd_demo(_: argparse.Namespace) -> None:
    """Replay the Section 11B example on a throwaway paper database."""
    from .config import RiskConfig

    now = [utcnow()]
    tmp = Path(tempfile.mkdtemp()) / "demo.db"
    svc = BankrollService(tmp, "paper", clock=lambda: now[0])
    svc.initialise_bankroll(4000, ManualAuthorisation("demo", "demo", bankroll_phrase(4000)))
    # The demo uses a 10% per-bet cap so the table's round numbers are reachable.
    svc.update_risk_config(
        RiskConfig(max_stake_fraction=0.10, max_daily_loss_fraction=1.0, max_event_loss_fraction=1.0,
                   max_event_exposure_fraction=0.5, max_total_exposure_fraction=0.5,
                   max_decimal_odds="6.00"),
        ManualAuthorisation("demo", "demo", "AUTHORISE RISK CHANGE"),
    )
    n = [0]

    def cycle(stake: int, odds: str, outcome: str) -> None:
        n[0] += 1
        start = now[0] + timedelta(hours=2)
        svc.upsert_event(f"demo-{n[0]}", "Demo event", start)
        bet = svc.place_bet(BetProposal(
            f"demo-{n[0]}", f"demo-{n[0]}", "f1", "moneyline", "Fighter A", Decimal(odds), stake,
            "demo", now[0], 0.55, 0.38))
        now[0] = start + timedelta(hours=1)
        svc.settle_bet(bet.bet_id, outcome, "demo", verified=True)

    def row(label: str) -> None:
        b = svc.balances()
        print(f"  {label:<38} {fmt(b.active_pence):>8} {fmt(b.protected_pence):>9}")

    print(f"\n  {'Event':<38} {'Active':>8} {'Protected':>9}")
    row("Initial deposit")
    cycle(400, "3.50", "WON")
    row("Win: total funds reach £50")
    cycle(300, "2.50", "LOST")
    cycle(200, "2.50", "LOST")
    row("£5 net loss")
    cycle(250, "5.00", "WON")
    row("£10 net profit")
    for stake in (400, 350, 300, 250, 200):
        cycle(stake, "2.50", "LOST")
    row("£15 net loss")
    print("\n  Protected profit was never touched by later losses.")
    svc.verify_integrity()
    print("  Ledger reconciled: every transaction balances.\n")
    svc.close()


def cmd_init(a: argparse.Namespace) -> None:
    svc = _service(a.mode)
    pence = pounds_to_pence(a.bankroll)
    svc.initialise_bankroll(pence, _ask_authorisation(bankroll_phrase(pence)))
    _print_status(svc)


def cmd_status(a: argparse.Namespace) -> None:
    _print_status(_service(a.mode))


def cmd_kill(a: argparse.Namespace) -> None:
    svc = _service(a.mode)
    svc.engage_kill_switch(a.reason)
    _print_status(svc)


def cmd_release_kill(a: argparse.Namespace) -> None:
    svc = _service(a.mode)
    svc.release_kill_switch(_ask_authorisation("RELEASE KILL SWITCH"))
    _print_status(svc)


def cmd_new_bankroll(a: argparse.Namespace) -> None:
    svc = _service(a.mode)
    deposit = pounds_to_pence(a.deposit)
    new_cap = svc.balances().active_pence + deposit
    svc.authorise_new_bankroll(deposit, _ask_authorisation(bankroll_phrase(new_cap)))
    _print_status(svc)


def cmd_verify(a: argparse.Namespace) -> None:
    svc = _service(a.mode)
    svc.verify_integrity()
    print("Ledger reconciled: OK")


STATS_DB = DATA_DIR / "ufc.db"
CSV_DIR = DATA_DIR / "raw" / "ufcstats_csv"


def cmd_update_data(a: argparse.Namespace) -> None:
    """Download the latest UFCStats history (CSV export) and rebuild data/ufc.db."""
    # Imported here so the ledger commands work without the data dependencies.
    from .data.http import Fetcher
    from .data.store import StatsStore
    from .data.ufcstats_csv import SOURCE_NOTE, download_csvs, load_csvs

    fetcher = Fetcher(CSV_DIR, min_interval=0.5, offline=a.offline)
    print(f"Downloading {SOURCE_NOTE} ...")
    csv_dir = download_csvs(fetcher)
    store = StatsStore(STATS_DB)
    try:
        r = load_csvs(store, csv_dir)
        print(f"Loaded {r.events} events, {r.fights} fights, {r.fighters} fighters. Newest event: {r.newest_event}")
        if r.matched_normalised:
            print(f"  {len(r.matched_normalised)} names matched after normalising spelling")
        if r.namesakes_resolved:
            print(f"  {len(r.namesakes_resolved)} shared names told apart by weight class: "
                  + ", ".join(sorted(r.namesakes_resolved)))
        if r.placeholders:
            print(f"  {len(r.placeholders)} fighters missing from the profile file "
                  "(fights kept; height, reach and age unknown)")
        if r.skipped:
            print(f"  {len(r.skipped)} bouts skipped:")
            for line in r.skipped[:10]:
                print("    -", line)
        _print_data_summary(store)
    finally:
        store.close()


def cmd_build_features(_: argparse.Namespace) -> None:
    import sqlite3

    from .features.build import attach_market_odds, build_fight_features

    conn = sqlite3.connect(str(STATS_DB))
    try:
        df = attach_market_odds(build_fight_features(conn), conn)
    finally:
        conn.close()
    if df.empty:
        print("No fights in the database yet. Run: python -m ufc_edge update-data")
        return
    out = DATA_DIR / "features.csv"
    df.to_csv(out, index=False)
    labelled = df["a_wins"].notna().sum()
    print(f"Wrote {out}: {len(df)} fights x {df.shape[1]} columns "
          f"({labelled} with a result, {int(df['is_upcoming'].sum())} upcoming).")
    print(f"Date range: {df['date'].min():%Y-%m-%d} to {df['date'].max():%Y-%m-%d}")
    if "market_p_a" in df:
        print(f"Market odds attached for {int(df['market_p_a'].notna().sum())} fights.")
    else:
        print("No historical odds loaded yet (run: python -m ufc_edge load-odds).")


def cmd_load_odds(a: argparse.Namespace) -> None:
    import sqlite3

    from .data.kaggle_odds import load_kaggle_odds

    path = Path(a.file)
    if not path.exists():
        print(f"Can't find {path}. Download ufc-master.csv from the Kaggle 'Ultimate UFC Dataset' "
              "and put it in data/raw/.")
        return
    conn = sqlite3.connect(str(STATS_DB))
    try:
        r = load_kaggle_odds(conn, path)
    finally:
        conn.close()
    with_odds = r.rows - r.no_odds
    print(f"{path.name}: {r.rows} fights ({r.first_date} to {r.last_date}), {with_odds} with moneyline odds.")
    print(f"Matched to the fight database: {r.matched} ({100 * r.matched / max(with_odds, 1):.1f}%), "
          f"of which {len(r.matched_loosely)} via a changed or variant name.")
    if r.unmatched:
        print(f"{len(r.unmatched)} not matched (names changed beyond recognition), e.g.:")
        for line in r.unmatched[:5]:
            print("  -", line)
    print("Next: python -m ufc_edge build-features")


def _print_data_summary(store) -> None:  # type: ignore[no-untyped-def]
    s = store.summary()
    print(f"\nDatabase {STATS_DB}: {s['events']} events, {s['fights']} fights "
          f"({s['fights_with_stats']} with stats), {s['fighters']} fighters, "
          f"{s['first_date']} to {s['last_date']}, {s['upcoming_bouts']} upcoming bouts\n")


def cmd_odds(_: argparse.Namespace) -> None:
    import sqlite3

    from .data.odds_api import OddsClient, load_api_key, take_snapshot

    DATA_DIR.mkdir(exist_ok=True)
    conn = sqlite3.connect(str(STATS_DB))
    try:
        r = take_snapshot(conn, OddsClient(load_api_key()), raw_dir=DATA_DIR / "raw" / "odds")
    finally:
        conn.close()
    print(f"Odds snapshot {r.captured_at}: {r.events} MMA fights, {r.prices_saved} new prices saved.")
    print(f"API requests remaining this month: {r.requests_remaining or 'unknown'}")


MODEL_PATH = DATA_DIR / "models" / "ufc_model.pkl"
REPORT_DIR = DATA_DIR / "reports"


def _features_with_odds():  # type: ignore[no-untyped-def]
    import sqlite3

    from .features.build import attach_market_odds, build_fight_features

    conn = sqlite3.connect(str(STATS_DB))
    try:
        return attach_market_odds(build_fight_features(conn, include_upcoming=False), conn)
    finally:
        conn.close()


def cmd_train(a: argparse.Namespace) -> None:
    from .model.artifact import save, train_final
    from .model.report import build_report

    df = _features_with_odds()
    if df.empty:
        print("No fight data yet. Run: python -m ufc_edge update-data")
        return
    print("Walk-forward evaluation (each year predicted by models trained only on earlier years)...")
    from .model.walkforward import walk_forward

    wf = walk_forward(df, first_test_year=a.first_year, log=print)
    print("Training the final model on all fights...")
    bundle = train_final(df, wf)
    report, summary = build_report(wf, trained=vars(bundle))
    bundle.evaluation = summary
    save(bundle, MODEL_PATH)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    (REPORT_DIR / "model_report.md").write_text(report, encoding="utf-8")
    wf.predictions.to_csv(REPORT_DIR / "walkforward_predictions.csv", index=False)
    print(f"\nLog-loss: market {summary['market_log_loss']:.4f}, model {summary['final_log_loss']:.4f} "
          f"({'better' if summary['beats_market'] else 'not better'} than the market; lower is better)")
    if summary["preview_bets"]:
        lo, hi = summary["preview_ci"]
        print(f"Betting preview: {summary['preview_bets']} bets, ROI {summary['preview_roi']:+.1%} "
              f"(95% range {lo:+.1%} to {hi:+.1%}). Indicative only; see the report.")
    print(f"Model saved to {MODEL_PATH}")
    print(f"Full report: {REPORT_DIR / 'model_report.md'}")


def cmd_predict(a: argparse.Namespace) -> None:  # noqa: C901
    import sqlite3

    from .model.artifact import load
    from .model.upcoming import predict_card

    if not MODEL_PATH.exists():
        print("No trained model yet. Run: python -m ufc_edge train")
        return
    bundle = load(MODEL_PATH)
    conn = sqlite3.connect(str(STATS_DB))
    try:
        preds = predict_card(conn, bundle)
    finally:
        conn.close()
    if preds.empty:
        print("No upcoming UFC fights with odds. Run: python -m ufc_edge odds")
        return
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    out = REPORT_DIR / "upcoming_predictions.csv"
    preds.to_csv(out, index=False)
    if getattr(a, "quiet", False):
        print(f"     {len(preds)} upcoming fights predicted, "
              f"{int((preds['value_flag'].fillna('') != '').sum())} with possible value.")
        return
    print(f"Model trained on fights up to {bundle.data_through}. Odds from {preds['odds_captured_at'].max()}.\n")
    for _, r in preds.iterrows():
        fav, p = (r["fighter_1"], r["p_model_1"]) if r["p_model_1"] >= 0.5 else (r["fighter_2"], 1 - r["p_model_1"])
        mkt = r["p_market_1"] if fav == r["fighter_1"] else 1 - r["p_market_1"]
        print(f"{r['date']}  {r['fighter_1']} vs {r['fighter_2']}")
        print(f"    Pick: {fav} {p:.0%}  (market {mkt:.0%}, stats-only "
              f"{(r['p_stats_1'] if fav == r['fighter_1'] else 1 - r['p_stats_1']):.0%})")
        print(f"    Best odds: {r['fighter_1']} {r['best_odds_1']:.2f} ({r['best_book_1']}), "
              f"{r['fighter_2']} {r['best_odds_2']:.2f} ({r['best_book_2']})")
        if r["value_flag"]:
            ev = r["ev_1"] if r["value_flag"] == r["fighter_1"] else r["ev_2"]
            print(f"    Possible value: {r['value_flag']} (estimated EV {ev:+.1%} per £1). Not a bet until Stage 4.")
        if r["factors"]:
            print(f"    Factors: {r['factors']}")
        if r["warnings"]:
            print(f"    ⚠ {r['warnings']}")
    print(f"\nSaved to {out} (includes method-of-victory probabilities).")


def cmd_backtest(_: argparse.Namespace) -> None:
    import json

    import pandas as pd

    from .backtest import run_all_variants

    wf_path = REPORT_DIR / "walkforward_predictions.csv"
    if not wf_path.exists():
        print("Run `python -m ufc_edge train` first (the backtest uses its walk-forward predictions).")
        return
    import sqlite3

    preds = pd.read_csv(wf_path)
    conn = sqlite3.connect(str(STATS_DB))
    try:
        events = pd.read_sql_query("SELECT fight_id, event_id FROM fights", conn)
    finally:
        conn.close()
    preds = preds.merge(events, on="fight_id", how="left")
    svc = _service("paper")
    try:
        base = svc.risk_config()
    finally:
        svc.close()
    print("Replaying every event since 2014 through the real strategy, risk engine and ledger...")
    res = run_all_variants(preds, base)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    (REPORT_DIR / "backtest.json").write_text(json.dumps(res, default=str), encoding="utf-8")
    for key, v in res["variants"].items():
        s = v["summary"]
        if not s["bets"]:
            print(f"\n{v['label']}: no bets qualified.")
            continue
        print(f"\n{v['label']}")
        print(f"  {s['bets']} bets, staked {fmt(s['staked_pence'])}, profit {fmt(s['pnl_pence'])} "
              f"({s['roi_on_stake']:+.1%} of stakes; 95% range {s['roi_ci_low']:+.1%} to {s['roi_ci_high']:+.1%})")
        print(f"  £40 became {fmt(s['final_active_pence'])} active + {fmt(s['final_protected_pence'])} protected. "
              f"Worst drawdown {fmt(-s['max_drawdown_pence'])}, longest losing run {s['longest_losing_streak']}.")
    print(f"\nSaved to {REPORT_DIR / 'backtest.json'} (shown on the dashboard's Performance page).")


def cmd_weekly(a: argparse.Namespace) -> None:
    """Everything for fight week in one go."""
    import sqlite3
    from types import SimpleNamespace

    import pandas as pd

    from .model.artifact import load
    from .settlement import settle_open_bets
    from .strategy import build_plan, dry_run, record_paper_bets

    if not a.skip_update:
        print("1/5  Updating fight results ...")
        cmd_update_data(SimpleNamespace(offline=False))
    stats = sqlite3.connect(str(STATS_DB))
    stats.row_factory = sqlite3.Row
    try:
        print("2/5  Settling finished bets ...")
        for mode in ("paper", "live"):
            if not (DATA_DIR / f"{mode}.db").exists():
                continue
            svc = _service(mode)
            try:
                if svc.status()["status"] == "UNINITIALISED":
                    continue
                rep = settle_open_bets(svc, stats)
                for line in rep.settled:
                    print(f"     [{mode}] settled {line}")
                for line in rep.problems:
                    print(f"     [{mode}] PROBLEM {line}")
                if rep.waiting:
                    print(f"     [{mode}] {len(rep.waiting)} bet(s) still waiting for results")
            finally:
                svc.close()
    finally:
        stats.close()

    print("3/5  Snapshotting current UK odds ...")
    cmd_odds(SimpleNamespace())
    stats = sqlite3.connect(str(STATS_DB))
    try:
        newest = stats.execute("SELECT MAX(date) FROM fights").fetchone()[0]
    finally:
        stats.close()
    bundle = load(MODEL_PATH) if MODEL_PATH.exists() else None
    if bundle is None or (newest and bundle.data_through < newest):
        print("4/5  New results since the model was trained, so retraining (1-2 minutes) ...")
        cmd_train(SimpleNamespace(first_year=2012))
        cmd_backtest(SimpleNamespace())
    else:
        print("4/5  Model is up to date.")
        if not (REPORT_DIR / "backtest.json").exists():
            cmd_backtest(SimpleNamespace())
    print("5/5  Predicting and building the betting plan ...")
    cmd_predict(SimpleNamespace(quiet=True))
    preds_path = REPORT_DIR / "upcoming_predictions.csv"
    if not preds_path.exists():
        return
    preds = pd.read_csv(preds_path)
    paper = _service("paper")
    try:
        if paper.status()["status"] == "UNINITIALISED":
            print("\nPaper bankroll not set up yet. Run: python -m ufc_edge init --bankroll 40")
            return
        plan = record_paper_bets(paper, build_plan(preds, paper))
        print(f"\nPaper trading: {sum(b.status == 'recorded' for b in plan)} new paper bet(s) recorded, "
              f"{sum(b.status == 'rejected' for b in plan)} rejected by the risk engine.")
        for b in plan:
            tag = {"recorded": "BET", "already recorded": "had", "rejected": "no "}[b.status]
            why = f"  ({'; '.join(b.reasons)})" if b.reasons else ""
            print(f"   {tag}  {b.fighter_name} @ {b.proposal.decimal_odds} ({b.bookmaker}), "
                  f"stake {fmt(b.proposal.stake_pence)}, edge {b.ev:+.1%}{why}")
        if not plan:
            print("   No fight met the minimum edge. Placing no bets is the correct decision.")
    finally:
        paper.close()
    if (DATA_DIR / "live.db").exists():
        live = _service("live")
        try:
            if live.status()["status"] != "UNINITIALISED":
                plan = dry_run(live, build_plan(preds, live))
                ok = [b for b in plan if b.status == "approved"]
                print(f"\nLive plan: {len(ok)} bet(s) approved by the risk engine. Review them on the dashboard.")
        finally:
            live.close()
    print("\nOpen the dashboard to review: python -m ufc_edge dashboard")


def cmd_experiment(a: argparse.Namespace) -> None:
    """PAPER ONLY: place the safest bets on the next card beyond your risk limits."""
    import pandas as pd

    from .strategy import experiment_plan, record_experiment

    preds_path = REPORT_DIR / "upcoming_predictions.csv"
    if not preds_path.exists():
        print("No predictions yet. Run: python -m ufc_edge weekly")
        return
    svc = _service("paper")
    try:
        plan = experiment_plan(pd.read_csv(preds_path), svc, n=a.bets, min_p=a.min_chance)
        if not plan:
            print("No favourite on the next card meets the experiment's rules.")
            return
        print(f"PAPER EXPERIMENT, beyond your risk limits: {len(plan)} bets, "
              f"{fmt(plan[0].proposal.stake_pence)} each.\n")
        for b in record_experiment(svc, plan):
            label = {"recorded": "PLACED", "already recorded": "HAD   ", "rejected": "REFUSED"}[b.status]
            print(f"  {label} {b.fighter_name} to beat {b.opponent_name} @ {b.proposal.decimal_odds} "
                  f"({b.bookmaker}), stake {fmt(b.proposal.stake_pence)}, model {b.p_model:.0%}, edge {b.ev:+.1%}")
            if b.status == "rejected":
                print(f"         reason: {'; '.join(b.reasons)}")
        print("\nThese are tagged 'Beyond risk limits' on the dashboard and settle with `weekly` after the event.")
    finally:
        svc.close()


def cmd_dashboard(a: argparse.Namespace) -> None:
    from .dashboard.server import serve

    serve(DATA_DIR, port=a.port, open_browser=not a.no_browser)


def cmd_data_summary(_: argparse.Namespace) -> None:
    from .data.store import StatsStore

    store = StatsStore(STATS_DB)
    _print_data_summary(store)
    store.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ufc_edge", description="UFC Edge operator console")
    sub = parser.add_subparsers(dest="command", required=True)

    def add(name: str, fn, help_: str) -> argparse.ArgumentParser:  # type: ignore[no-untyped-def]
        p = sub.add_parser(name, help=help_)
        p.add_argument("--mode", choices=("paper", "live"), default="paper")
        p.set_defaults(fn=fn)
        return p

    add("demo", cmd_demo, "replay the Section 11B example on a throwaway database")
    add("init", cmd_init, "authorise the initial bankroll").add_argument("--bankroll", required=True)
    add("status", cmd_status, "show balances and system status")
    add("kill", cmd_kill, "engage the emergency stop").add_argument("--reason", required=True)
    add("release-kill", cmd_release_kill, "release the emergency stop (needs authorisation)")
    add("new-bankroll", cmd_new_bankroll, "restart after exhaustion").add_argument("--deposit", required=True)
    add("verify", cmd_verify, "reconcile the ledger")

    sp = sub.add_parser("update-data", help="download the latest UFC fight history and rebuild the stats database")
    sp.add_argument("--offline", action="store_true", help="reuse the last downloaded files")
    sp.set_defaults(fn=cmd_update_data)
    sub.add_parser("build-features", help="write data/features.csv (point-in-time features)").set_defaults(
        fn=cmd_build_features)
    tr = sub.add_parser("train", help="walk-forward evaluation, then train and save the model")
    tr.add_argument("--first-year", type=int, default=2012, help="first year to test on")
    tr.set_defaults(fn=cmd_train)
    sub.add_parser("predict", help="predict upcoming fights from the latest odds snapshot").set_defaults(fn=cmd_predict)
    ex = sub.add_parser("experiment", help="PAPER ONLY: safest bets on the next card, beyond your risk limits")
    ex.add_argument("--bets", type=int, default=5)
    ex.add_argument("--min-chance", type=float, default=0.70)
    ex.set_defaults(fn=cmd_experiment)
    db = sub.add_parser("dashboard", help="open the dashboard in your browser")
    db.add_argument("--port", type=int, default=8765)
    db.add_argument("--no-browser", action="store_true")
    db.set_defaults(fn=cmd_dashboard)
    sub.add_parser("backtest", help="replay history through the real risk engine and ledger").set_defaults(
        fn=cmd_backtest)
    wk = sub.add_parser("weekly", help="fight week: update results, settle, odds, retrain if needed, plan")
    wk.add_argument("--skip-update", action="store_true", help="don't download new results first")
    wk.set_defaults(fn=cmd_weekly)
    lo = sub.add_parser("load-odds", help="load historical odds from Kaggle's ufc-master.csv")
    lo.add_argument("--file", default=str(DATA_DIR / "raw" / "ufc-master.csv"))
    lo.set_defaults(fn=cmd_load_odds)
    sub.add_parser("data-summary", help="what's in the stats database").set_defaults(fn=cmd_data_summary)
    sub.add_parser("odds", help="snapshot current UK MMA odds (needs ODDS_API_KEY in .env)").set_defaults(fn=cmd_odds)

    args = parser.parse_args(argv)
    try:
        args.fn(args)
    except UfcEdgeError as exc:
        print(f"Refused: {exc}", file=sys.stderr)
        return 1
    return 0
