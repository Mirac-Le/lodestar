"""LLM-based attribute & relationship enrichment.

The `enrich` package is responsible for parsing the rich free-text fields
already attached to each Person (bio / notes / tags / context) into
clean structured attributes (companies / cities / titles / extra tags),
and — in a second pass — into peer-to-peer edges.

Privacy model:
  Every request to the cloud LLM goes through `Anonymizer`, which
  rewrites in-table person names (and the row's own name) to opaque
  `Pxxx` tokens, AND known company names (any `companies[]` value
  already structured under any contact in this owner's roster) to
  `Cxxx` tokens, before the prompt leaves the machine. The reverse map
  lives only in process memory for the duration of one call.

  Caveat: companies that the user has not yet structured (i.e. only
  appear as raw text inside `bio` / `notes`) cannot be anonymized,
  because L1's *job* is to surface them. As soon as L1 stores a new
  company under `person.companies`, subsequent runs treat it as a
  known `Cxxx` entity.
"""

from lodestar.enrich.anonymizer import Anonymizer
from lodestar.enrich.client import LLMClient, LLMError
from lodestar.enrich.extractor import L1Extractor, L1Result
from lodestar.enrich.normalizer import (
    BUILTIN_ALIASES,
    AliasGroup,
    build_groups,
    cluster_with_llm,
    load_alias_file,
)
from lodestar.enrich.relationship_parser import (
    ProposedEdge,
    RelationshipParseResult,
    RelationshipParser,
)

__all__ = [
    "AliasGroup",
    "Anonymizer",
    "BUILTIN_ALIASES",
    "L1Extractor",
    "L1Result",
    "LLMClient",
    "LLMError",
    "ProposedEdge",
    "RelationshipParseResult",
    "RelationshipParser",
    "build_groups",
    "cluster_with_llm",
    "load_alias_file",
]
