"""Ingestion adapters and shared ingest helpers for confirmed-ctl.

The concrete BofA email-scan / export adapters land in a later generation; this
package currently hosts only the shared dedup helper used to populate
``bank_transactions.source_txn_id`` deterministically.
"""
