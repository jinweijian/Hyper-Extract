from pydantic import BaseModel, ConfigDict, Field


class RunCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    request_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    request_json: dict[str, object]
    output_uri: str
