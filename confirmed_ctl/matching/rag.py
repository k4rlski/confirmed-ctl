"""confirmed_ctl/matching/rag.py

ChromaDB-backed pattern memory for confirmed matches.
Each confirmed match is embedded and stored. At query time,
similar ads retrieve similar past matches for ranking context.

ChromaDB is imported lazily so the rest of the package (and the scorer, which
works standalone) imports without it installed.
"""

from __future__ import annotations

from .. import settings

COLLECTION = "confirmed_matches"


def get_collection():
    import chromadb
    from chromadb.utils import embedding_functions

    client = chromadb.PersistentClient(path=settings.CHROMA_PATH)
    ef = embedding_functions.DefaultEmbeddingFunction()
    return client.get_or_create_collection(
        name=COLLECTION,
        embedding_function=ef,
    )


def store_confirmed_match(
    ad_id: int,
    ad_number: str,
    newspaper_name: str,
    expected_amount: float,
    txn_amount: float,
    txn_date: str,
    txn_vendor: str,
    match_method: str,
):
    """
    Called after a human confirms a match. Embeds the pattern for future retrieval.
    """
    col = get_collection()
    doc_text = (
        f"Newspaper: {newspaper_name}. "
        f"Expected amount: {expected_amount}. "
        f"Actual charge: {txn_amount} on {txn_date} from vendor '{txn_vendor}'. "
        f"Matched via: {match_method}."
    )
    col.add(
        documents=[doc_text],
        ids=[f"ad_{ad_id}"],
        metadatas=[{
            "ad_id": ad_id,
            "ad_number": ad_number,
            "newspaper_name": newspaper_name,
            "txn_vendor": txn_vendor,
            "match_method": match_method,
        }],
    )


def retrieve_similar_patterns(newspaper_name: str, expected_amount: float, n: int = 5):
    """
    Retrieve past confirmed matches similar to this ad.
    Used to boost scoring for vendors/amounts we've seen before.
    """
    col = get_collection()
    query_text = f"Newspaper: {newspaper_name}. Expected amount: {expected_amount}."
    results = col.query(query_texts=[query_text], n_results=n)
    return results.get("metadatas", [[]])[0]
