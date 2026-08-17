"""SageMath stub generation helpers."""

from .generator import GenerationSummary, generate
from .preparser import PreparseResult, preparse_path, preparse_source

# Keep in sync with pyproject.toml; used as the fallback when the package is
# run from a source checkout (importlib.metadata has no installed version).
__version__ = "0.8.1"

__all__ = [
    "GenerationSummary",
    "PreparseResult",
    "generate",
    "preparse_path",
    "preparse_source",
    "__version__",
]
