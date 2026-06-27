"""Output schema parser."""

from typing import List, Optional
from .schemas import (
    FieldSchema,
    NaiveOutputSchema,
    GraphOutputSchema,
)
from pydantic import Field, create_model, BaseModel


TYPE_MAPPING = {
    "str": str,
    "int": int,
    "float": float,
    "bool": bool,
    "list": List[str],
}


def build_naive_schema(
    name: str,
    fields: List[FieldSchema],
    description: str,
) -> BaseModel:
    """Build Pydantic schema for naive types (model, list, set)."""
    schema_fields = {}
    for field in fields:
        kwargs = {
            "description": field.description,
        }
        if field.default is not None:
            kwargs["default"] = field.default
        elif field.required is False:  # 非必填且无默认值时，设 default=None
            kwargs["default"] = None
        # Use Optional type for non-required fields so None values pass validation
        base_type = TYPE_MAPPING[field.type]
        if field.required is False:
            field_type = Optional[base_type]
        else:
            field_type = base_type
        schema_fields[field.name] = (
            field_type,
            Field(**kwargs),
        )
    return create_model(name, __doc__=description, **schema_fields)


def build_graph_schema(
    entity: NaiveOutputSchema,
    relation: NaiveOutputSchema,
    description: str,
) -> tuple[BaseModel, BaseModel]:
    """Build Pydantic schema for graph types (graph, hypergraph, temporal_graph, spatial_graph, spatio_temporal_graph)."""
    entity_schema = build_naive_schema(
        "NodeSchema",
        entity.fields,
        entity.description,
    )
    relation_schema = build_naive_schema(
        "EdgeSchema",
        relation.fields,
        relation.description,
    )
    return entity_schema, relation_schema


def parse_output(
    output: NaiveOutputSchema | GraphOutputSchema, autotype: str
) -> BaseModel:
    """Parse template and return schemas based on autotype.

    Args:
        config: TemplateCfg instance

    """

    if autotype in ("model", "list", "set"):
        return build_naive_schema(
            "DataSchema",
            output.fields,
            output.description,
        )
    else:
        return build_graph_schema(
            output.entities,
            output.relations,
            output.description,
        )


__all__ = [
    "parse_output",
]
