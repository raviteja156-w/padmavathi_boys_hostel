"""Regenerate migrations/0001_initial.sql from models.py (PostgreSQL dialect, IF NOT EXISTS only).

Usage:  python tools/gen_migration.py > migrations/0001_initial.sql
For later schema changes, add a NEW numbered .sql file by hand; never edit applied migrations.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex, CreateTable

from models import db

d = postgresql.dialect()
print("-- NEXORA WORKPULSE initial schema. Safe: creates only missing nwp_* objects, never drops or alters.")
for table in db.metadata.sorted_tables:
    print(str(CreateTable(table, if_not_exists=True).compile(dialect=d)).strip() + ";\n")
    for ix in sorted(table.indexes, key=lambda i: i.name):
        print(str(CreateIndex(ix, if_not_exists=True).compile(dialect=d)).strip() + ";\n")
