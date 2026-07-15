import pytest
from pydantic import BaseModel

from hyperextract.providers.contracts import (
    EmbeddingItemResult,
    EmbeddingResponse,
    GenerationResponse,
)
from hyperextract.providers.langchain import (
    AdapterChatModel,
    AdapterEmbeddings,
    EmbeddingQuarantineError,
)
from hyperextract.providers.profiles import ModelProfile, ProfileCapabilities


class PartialAdapter:
    name = "partial"

    def embed(self, request):
        return EmbeddingResponse(
            request_id=request.request_id,
            items=[
                EmbeddingItemResult(
                    input_index=0,
                    vector=[1.0, 2.0],
                    status="completed",
                ),
                EmbeddingItemResult(
                    input_index=1,
                    vector=None,
                    status="quarantined",
                    error_reason="bad_request",
                ),
            ],
        )


def test_adapter_embeddings_exposes_partial_response_without_invalid_vectors():
    embeddings = AdapterEmbeddings(PartialAdapter())

    response = embeddings.embed_with_status(["good", "bad"])

    assert [item.vector for item in response.items] == [[1.0, 2.0], None]
    assert embeddings.last_response.items[1].status == "quarantined"


def test_adapter_embeddings_vector_only_api_rejects_quarantined_positions():
    embeddings = AdapterEmbeddings(PartialAdapter())

    with pytest.raises(EmbeddingQuarantineError) as error:
        embeddings.embed_documents(["good", "bad"])

    assert error.value.response.items[1].status == "quarantined"


class Answer(BaseModel):
    answer: str


def test_adapter_chat_model_structured_output_stays_on_gateway():
    profile = ModelProfile(
        name="chat-wrapper",
        llm="openai:model@https://example.test/v1",
        llm_api_key_env="TEST_KEY",
        capabilities=ProfileCapabilities(
            structured_output_modes=["text_json"],
            preferred_structured_output_mode="text_json",
        ),
    )
    requests = []

    class Gateway:
        def __init__(self):
            self.profile = profile

        def invoke(self, request):
            requests.append(request)
            return GenerationResponse(
                request_id=request.request_id,
                final_text='{"answer":"gateway"}',
            )

    model = AdapterChatModel(Gateway(), model_name="model")

    result = model.with_structured_output(
        Answer, method="json_schema"
    ).invoke("question")

    assert result == Answer(answer="gateway")
    assert requests[0].structured_output is True
    assert requests[0].structured_output_mode == "text_json"
