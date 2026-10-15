from pydantic import BaseModel, Field
from typing import Literal

class BranchSpec(BaseModel):
    """single top level config from query-error.yaml,
    including exclusions
    """
    base: str = Field(min_length=1)      # filters only, no pipes
    key_field: str = Field(min_length=1)  # group key besides host
    fields: list[str] = Field(default_factory=list)  # '| fields' projection
    exclusions: list[str] = Field(default_factory=list)

    def filters(self, *extra: str) -> str:
        return " ".join([self.base, *self.exclusions, *extra])

    def compose(self, *extra: str) -> str:
        q = self.filters(*extra)
        return f"{q} | fields {', '.join(self.fields)}" if self.fields else q

    def stats_query(self) -> str:
        return self.filters() + f" | stats by (host, {self.key_field}) count(*) rows"


class ContextSpec(BaseModel):
    """ Used for correlation, from query-context.yaml
    Includes instructions for llm so it knows how to use them.
    Fields can be undefined, it will return whole line. 
    """
    query: str = Field(min_length=1)
    limit: int = 40
    instructions: str = ""
    fields: list[str] = Field(default_factory=list)


class Group(BaseModel):
    """Summary (count) of results per branch and key_field"""
    branch: str
    host: str
    key: str          # key_field value; "(no <field>)" when logs lack it
    count: int
    spec: BranchSpec = Field(exclude=True, repr=False)  # owning branch
    key_missing: bool = Field(default=False, exclude=True, repr=False)

    @property
    def gid(self) -> str:
        return f"{self.branch}:{self.host}:{self.key}"

    def scope(self) -> str:
        """AND-filter appended to the branch filters to select exactly this group."""
        if self.key_missing:
            return f'and host:"{self.host}"'
        return f'and (host:"{self.host}" and {self.spec.key_field}:"{self.key}")'

    def scoped_query(self) -> str:
        """The branch query narrowed to this group, with projection."""
        return self.spec.compose(self.scope())


class LogLine(BaseModel):
    t: str = ""
    msg: str = ""


class TopMessage(BaseModel):
    hits: int | str
    msg: str


class ContextResult(BaseModel):
    """ results of single context query, including
    instructions for llm
    """
    instructions: str = ""
    rows: list[dict] = Field(default_factory=list)
    error: str | None = None   # set when the query itself failed


class FullDetail(BaseModel):
    mode: Literal["full"] = "full"
    count: int
    truncated: bool
    logs: list[LogLine]


class SignatureDetail(BaseModel):
    mode: Literal["signatures"] = "signatures"
    count: int
    top_msgs: list[TopMessage] | None = None
    top_msgs_error: str | None = None
    hourly: dict[str, int] | None = None
    hourly_error: str | None = None
    samples: list[LogLine] | None = None
    samples_note: str | None = None
    samples_error: str | None = None


class ErrorDetail(BaseModel):
    error: str
    count: int


class Report(BaseModel):
    window: str
    start: str
    end: str
    groups: list[Group]
    excluded_from_full: list[str]
    context: dict[str, ContextResult]
    detail: dict[str, FullDetail | SignatureDetail | ErrorDetail]
