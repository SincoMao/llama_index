import pytest
from llama_index.ingestion.transformation import (
    ConfigurableTransformations,
    ConfiguredTransformation,
)

from llama_index.node_parser import SimpleNodeParser, SentenceWindowNodeParser


@pytest.mark.parametrize(
    "configurable_transformation_type", ConfigurableTransformations
)
def test_can_generate_schema_for_transformation_component_type(
    configurable_transformation_type: ConfigurableTransformations,
) -> None:
    schema = configurable_transformation_type.value.schema()  # type: ignore
    assert schema is not None
    assert len(schema) > 0

    # also check that we can generate schemas for
    # ConfiguredTransformation[component_type]
    component_type = configurable_transformation_type.value.component_type
    configured_schema = ConfiguredTransformation[
        component_type  # type: ignore
    ].schema()
    assert configured_schema is not None
    assert len(configured_schema) > 0


def test_can_build_configured_transform_from_component() -> None:
    parser = SimpleNodeParser.from_defaults()
    configured_transformation = ConfiguredTransformation.from_component(parser)
    assert isinstance(
        configured_transformation,
        ConfiguredTransformation[SimpleNodeParser],  # type: ignore
    )
    assert not isinstance(
        configured_transformation,
        ConfiguredTransformation[SentenceWindowNodeParser],  # type: ignore
    )
    assert (
        configured_transformation.configurable_transformation_type.value.component_type
        == SimpleNodeParser
    )


def test_build_configured_transformation() -> None:
    parser = SimpleNodeParser.from_defaults()
    configured_transformation = (
        ConfigurableTransformations.SIMPLE_NODE_PARSER.build_configured_transformation(
            parser
        )
    )
    assert isinstance(
        configured_transformation,
        ConfiguredTransformation[SimpleNodeParser],  # type: ignore
    )

    with pytest.raises(ValueError):
        ConfigurableTransformations.SENTENCE_WINDOW_NODE_PARSER.build_configured_transformation(
            parser
        )


def test_unique_configurable_transformations_names() -> None:
    names = set()
    for configurable_transformation_type in ConfigurableTransformations:
        assert configurable_transformation_type.value.name not in names
        names.add(configurable_transformation_type.value.name)
    assert len(names) > 0
    assert len(names) == len(ConfigurableTransformations)
