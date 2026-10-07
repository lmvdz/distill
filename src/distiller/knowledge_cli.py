"""Command line entry points for the general Distill pipeline."""

import argparse
import asyncio
import json
from pathlib import Path

from .demo import demonstration
from .directives import Directive, canonical, compile_request
from .distill_store import Registry
from .engine import execute, replay
from .library import FixtureAdapter, SQLiteAdapter
from .lineage import import_catalog
from .maintenance import acquire, maintain
from .migration import migrate
from .model import Model
from .publication import atomic_file, diff, export, search


def add_arguments(parser: argparse.ArgumentParser):
    parser.add_argument("--registry", type=Path, help="defaults to data/distill.sqlite")
    parser.add_argument("--output", type=Path, help="defaults to data/catalogs")
    sub = parser.add_subparsers(dest="action", required=True)
    create = sub.add_parser("create", help="compile a request and store an inspectable directive")
    create.add_argument("request")
    create.add_argument("--key", required=True)
    create.add_argument("--catalog")
    create.add_argument("--save", type=Path)
    load = sub.add_parser("load", help="validate/store or revise a directive from JSON")
    load.add_argument("file", type=Path)
    show = sub.add_parser("show", help="show the directive and its explicit defaults")
    show.add_argument("key")
    show.add_argument("--version")
    sub.add_parser("status", help="directives, publication versions, attempts and errors")
    sub.add_parser("schema", help="emit the validated directive JSON schema")
    sync = sub.add_parser("sync", help="read a fixture library; never mutate upstream metadata")
    sync.add_argument("file", type=Path)
    sub.add_parser(
        "import-catalog", help="import original evidence and catalog lineage"
    ).add_argument("manifest", type=Path)
    sub.add_parser("migrate", help="additively map legacy cards and source links").add_argument(
        "database", type=Path
    )
    sql = sub.add_parser(
        "sync-sql", help="read a bookmark SQL database via a configurable projection"
    )
    sql.add_argument("database", type=Path)
    sql.add_argument("--library", required=True)
    sql.add_argument("--query", type=Path, required=True, help="SELECT projecting item_json")
    sql.add_argument("--incremental", action="store_true", help="query uses :cursor/change_cursor")
    sql.add_argument("--max-items", type=int, default=10000)
    for action in ("run", "pause", "resume", "retire", "retry"):
        sub.add_parser(action).add_argument("key")
    re = sub.add_parser("replay", help="offline semantic replay, without providers or discovery")
    re.add_argument("manifest", type=Path)
    ex = sub.add_parser("export", help="export the current complete catalog")
    ex.add_argument("catalog")
    query = sub.add_parser("search", help="local lexical search over a general catalog")
    query.add_argument("catalog")
    query.add_argument("query")
    query.add_argument("--limit", type=int, default=10)
    changes = sub.add_parser("diff", help="compare two manifest files")
    changes.add_argument("before", type=Path)
    changes.add_argument("after", type=Path)
    service = sub.add_parser("maintain", help="fair scheduled runs with shared persisted budgets")
    service.add_argument("--once", action="store_true")
    service.add_argument("--stop", action="store_true")
    service.add_argument("--interval", type=int, default=30)
    service.add_argument("--limit", type=int, default=10)
    service.add_argument("--daily-cap", type=int, default=500)
    sub.add_parser(
        "demo", help="bounded synthetic shared-source/two-directive/offline replay proof"
    )


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


async def with_model(settings, operation):
    model = Model(settings)
    try:
        return await operation(model)
    finally:
        await model.close()


def print_json(value):
    print(json.dumps(value, indent=2, ensure_ascii=False))


def current(store: Registry, catalog: str) -> dict:
    manifest = store.head(catalog)
    if not manifest:
        raise ValueError(f"catalog has no complete publication: {catalog}")
    return manifest


def offline_action(store: Registry, args, root: Path) -> bool:
    if args.action in {"load", "show", "status", "schema"}:
        inspect_action(store, args)
    elif args.action in {"sync", "sync-sql", "import-catalog", "migrate"}:
        import_action(store, args)
    elif args.action in {"pause", "resume", "retire", "retry"}:
        state_action(store, args)
    elif args.action == "replay":
        result = asyncio.run(replay(read_json(args.manifest)))
        print_json(
            {"replay": "identical", "catalog": result["catalog"], "records": len(result["records"])}
        )
    elif args.action == "export":
        print(export(current(store, args.catalog), root))
    elif args.action == "search":
        print_json(search(current(store, args.catalog), args.query, args.limit))
    elif args.action == "diff":
        print_json(diff(read_json(args.before), read_json(args.after)))
    elif args.action == "demo":
        print_json(asyncio.run(demonstration(store, root)))
    else:
        return False
    return True


def inspect_action(store: Registry, args):
    if args.action == "load":
        directive = Directive.model_validate(read_json(args.file))
        store.save_directive(directive)
        print_json({"version": directive.version, "directive": directive.model_dump()})
    elif args.action == "show":
        directive = store.directive(args.key, args.version)
        print_json({"version": directive.version, "directive": directive.model_dump()})
    elif args.action == "status":
        print_json(store.report())
    elif args.action == "schema":
        print_json(Directive.model_json_schema())


def import_action(store: Registry, args):
    if args.action == "sync":
        adapter = FixtureAdapter(args.file)
        initial = adapter.changes()
        batch = adapter.changes(store.cursor(initial.library))
        print_json({**store.import_batch(batch), "capabilities": adapter.capabilities()})
        store.configure_adapter(
            initial.library, {"kind": "fixture", "path": str(args.file.resolve())}
        )
    elif args.action == "sync-sql":
        adapter = SQLiteAdapter(
            args.database,
            args.library,
            args.query.read_text(encoding="utf-8"),
            full_snapshot=not args.incremental,
            max_items=args.max_items,
        )
        batch = adapter.changes(store.cursor(args.library))
        print_json({**store.import_batch(batch), "capabilities": adapter.capabilities()})
        store.configure_adapter(
            args.library,
            {
                "kind": "sqlite",
                "path": str(args.database.resolve()),
                "query": adapter.query,
                "full_snapshot": not args.incremental,
                "max_items": args.max_items,
            },
        )
    elif args.action == "import-catalog":
        print_json(asyncio.run(import_catalog(store, read_json(args.manifest))))
    elif args.action == "migrate":
        print_json(migrate(store, args.database))


def state_action(store: Registry, args):
    if args.action == "retry":
        store.directive(args.key)
        store.retry(args.key)
    else:
        store.set_state(
            args.key, {"pause": "paused", "resume": "active", "retire": "retired"}[args.action]
        )
    print_json({"directive": args.key, "action": args.action})


async def run_directive(store: Registry, args, settings, root: Path, model):
    directive = store.directive(args.key)
    if directive.model != settings.text_model:
        raise ValueError(
            f"directive requires model {directive.model}; configured {settings.text_model}"
        )
    await acquire(store, args.key)
    manifest = await execute(store, args.key, model)
    return {
        "run": manifest["id"],
        "records": len(manifest["semantic"]["records"]),
        "gallery": str(export(manifest, root)),
    }


def command(settings, args):
    path = args.registry or settings.data / "distill.sqlite"
    root = args.output or settings.data / "catalogs"
    store = Registry(path)
    try:
        if offline_action(store, args, root):
            return
        if args.action == "create":
            directive = asyncio.run(
                with_model(
                    settings,
                    lambda model: compile_request(
                        model,
                        args.request,
                        args.key,
                        settings.text_model,
                        args.catalog,
                        record=store.record_compilation,
                        inventory=store.inventory(),
                    ),
                )
            )
            store.save_directive(directive)
            if args.save:
                atomic_file(args.save, canonical(directive.model_dump()))
            print_json({"version": directive.version, "directive": directive.model_dump()})
        elif args.action == "run":
            print_json(
                asyncio.run(
                    with_model(
                        settings, lambda model: run_directive(store, args, settings, root, model)
                    )
                )
            )
        elif args.action == "maintain":
            stop = path.with_suffix(".stop")
            if args.stop:
                stop.touch()
                print("stop requested; current directive finishes before exit")
            else:
                stop.unlink(missing_ok=True)
                print_json(
                    asyncio.run(
                        with_model(
                            settings,
                            lambda model: maintain(
                                store,
                                model,
                                root,
                                stop,
                                once=args.once,
                                interval=args.interval,
                                limit=args.limit,
                                shared_cap=args.daily_cap,
                                model_name=settings.text_model,
                            ),
                        )
                    )
                )
    finally:
        store.close()
