"""Method Registry - Central registry for all extraction methods.

This module provides a unified registry for method templates, enabling
consistent creation and management of method-based knowledge extraction.
"""

from typing import Dict, Any, Type, Optional
from pydantic import BaseModel


_METHOD_REGISTRY: Dict[str, Dict[str, Any]] = {}


def register_method(
    name: str,
    method_class: Type,
    autotype: str,
    description: str = "",
    language: str = "en",
) -> None:
    """Register a method in the registry.

    Args:
        name: Method name (e.g., "light_rag")
        method_class: The method class
        autotype: AutoType category (e.g., "graph", "hypergraph")
        description: Method description
    """
    _METHOD_REGISTRY[name] = {
        "class": method_class,
        "type": autotype,
        "description": description,
        "language": language,
    }


def get_method(name: str) -> Optional[Dict[str, Any]]:
    """Get method info by name.

    Args:
        name: Method name

    Returns:
        Method info dict or None if not found
    """
    return _METHOD_REGISTRY.get(name)


def list_methods() -> Dict[str, Dict[str, Any]]:
    """List all registered methods.

    Returns:
        Dict mapping method names to their info
    """
    return _METHOD_REGISTRY.copy()


class MethodCfg(BaseModel):
    """Configuration model for method templates."""

    name: str
    type: str
    description: str = ""
    language: str = "en"


def get_method_cfg(name: str) -> Optional[MethodCfg]:
    """Get method configuration as MethodCfg.

    Args:
        name: Method name

    Returns:
        MethodCfg or None if not found
    """
    info = get_method(name)
    if info is None:
        return None
    return MethodCfg(
        name=name,
        type=info["type"],
        description=info["description"],
        language=info.get("language", "en"),
    )


def list_method_cfgs() -> Dict[str, MethodCfg]:
    """List all method configurations.

    Returns:
        Dict mapping "method/{name}" to MethodCfg
    """
    return {
        f"method/{name}": MethodCfg(
            name=name,
            type=info["type"],
            description=info["description"],
            language=info.get("language", "en"),
        )
        for name, info in _METHOD_REGISTRY.items()
    }


def _init_registry():
    """Initialize the registry with built-in methods."""
    from hyperextract.methods.rag import (
        Light_RAG,
        Hyper_RAG,
        HyperGraph_RAG,
        Cog_RAG,
        Graph_RAG,
        CourseKnowledgeGraph,
    )
    from hyperextract.methods.typical import iText2KG, iText2KG_Star, KG_Gen, Atom

    register_method(
        name="course_knowledge_graph",
        method_class=CourseKnowledgeGraph,
        autotype="graph",
        description="Section-aware course knowledge graph extraction for long documents",
        language="zh",
    )

    register_method(
        name="graph_rag",
        method_class=Graph_RAG,
        autotype="graph",
        description="Graph-RAG: Graph-based Retrieval-Augmented Generation with Community detection",
    )

    register_method(
        name="light_rag",
        method_class=Light_RAG,
        autotype="graph",
        description="Lightweight Graph-based RAG with binary edges for entity-relationship extraction",
    )

    register_method(
        name="hyper_rag",
        method_class=Hyper_RAG,
        autotype="hypergraph",
        description="Hypergraph-based RAG with n-ary hyperedges for complex multi-entity relationships",
    )

    register_method(
        name="hypergraph_rag",
        method_class=HyperGraph_RAG,
        autotype="hypergraph",
        description="HyperGraph RAG implementation for advanced knowledge graph construction",
    )

    register_method(
        name="cog_rag",
        method_class=Cog_RAG,
        autotype="hypergraph",
        description="Cognitive RAG implementation for intelligent knowledge retrieval",
    )

    register_method(
        name="itext2kg",
        method_class=iText2KG,
        autotype="graph",
        description="iText2KG: High-quality triple-based knowledge graph extraction",
    )

    register_method(
        name="itext2kg_star",
        method_class=iText2KG_Star,
        autotype="graph",
        description="iText2KG Star: Enhanced version with improved extraction quality",
    )

    register_method(
        name="kg_gen",
        method_class=KG_Gen,
        autotype="graph",
        description="KG_Gen: Knowledge Graph Generator for structured knowledge extraction",
    )

    register_method(
        name="atom",
        method_class=Atom,
        autotype="graph",
        description="Atom: Temporal knowledge graph extraction with evidence attribution",
    )


_init_registry()
