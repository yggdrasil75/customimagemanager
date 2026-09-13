"""
Embedding module — image embeddings, clustering, semantic search.
======================================================================

Provides whole-image embeddings (local CNN or OAI), clustering,
concept maps (heuristics), and semantic/text search.
"""

MANIFEST = {
    "id":          "embedding",
    "name":        "Image Embeddings",
    "version":     "1.0.0",
    "description": "Whole-image embeddings, clustering, concept maps, and semantic search. Adds the Review tab.",
    "core":        False,
    "requires":    [],
    "pip":         [],
    "assets":      ["embedding.js", "embedding.css"],
}


def register(host):
    from . import __init__ as embedding_module
    embedding_module.register(host)