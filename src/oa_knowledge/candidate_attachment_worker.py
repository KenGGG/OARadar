"""Isolated one-attachment converter for a classified candidate build."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from sqlalchemy.orm import sessionmaker

from oa_knowledge.backfill_mvp import BackfillMVPService, CanonicalAttachment
from oa_knowledge.classification.private_config import (
    load_private_classification_config,
)
from oa_knowledge.config import load_settings
from oa_knowledge.db import create_db_engine
from oa_knowledge.db.models import ArchivedFile


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--file-id", type=int, required=True)
    parser.add_argument("--alias-ids", required=True)
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--ordinal", type=int, required=True)
    args = parser.parse_args()
    settings = load_settings(args.config)
    loaded = load_private_classification_config(settings.classification_private_dir)
    factory = sessionmaker(create_db_engine(settings.database_path), expire_on_commit=False)
    with factory() as session:
        file = session.get(ArchivedFile, args.file_id)
        aliases = tuple(session.get(ArchivedFile, value) for value in json.loads(args.alias_ids))
        if file is None or any(value is None for value in aliases):
            raise SystemExit("attachment source missing")
        detached: set[int] = set()
        for value in (file, *aliases):
            if value.id not in detached:
                session.expunge(value)
                detached.add(value.id)
    service = BackfillMVPService(
        settings,
        factory,
        loaded.config,
        private_config_sha256=loaded.config_sha256,
    )
    outcome, filename, problem = service._convert_attachment(
        args.package,
        CanonicalAttachment(file, aliases),
        args.ordinal,
    )
    print(
        json.dumps(
            {"outcome": outcome, "filename": filename, "problem": problem},
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
