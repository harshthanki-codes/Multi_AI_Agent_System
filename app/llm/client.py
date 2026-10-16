import abc
import json
import logging
import random
import time
from typing import Dict, Any, Optional

import httpx
from openai import OpenAI, APIError, APITimeoutError, AuthenticationError

from app.core.config import settings
from app.core.state_machine import AgentRole
from app.schemas.session import VerdictStructuredOutput

logger = logging.getLogger(__name__)


class LLMProviderError(Exception):
    """Base exception for LLM provider errors."""
    pass


class LLMAuthenticationError(LLMProviderError):
    """Raised on authentication/bad key errors; must NOT be retried blindly."""
    pass


class LLMTimeoutError(LLMProviderError):
    """Raised when the LLM call times out."""
    pass


class LLMOutputParseError(LLMProviderError):
    """Raised when structured output cannot be parsed."""
    pass


class BaseLLMClient(abc.ABC):
    @abc.abstractmethod
    def generate(
        self,
        system_prompt: str,
        user_prompt: str,
        role: AgentRole,
        max_tokens: int = 600,
        temperature: float = 0.7,
    ) -> str:
        """Generate response text for an agent role."""
        pass

    @abc.abstractmethod
    def generate_structured_verdict(
        self,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int = 1000,
    ) -> VerdictStructuredOutput:
        """Generate validated structured verdict for Judge."""
        pass


class MockLLMClient(BaseLLMClient):
    """
    High-fidelity deterministic mock client for testing, offline runs, and CI.
    Generates role-specific, context-aware responses without requiring paid API tokens.
    """

    def generate(
        self,
        system_prompt: str,
        user_prompt: str,
        role: AgentRole,
        max_tokens: int = 600,
        temperature: float = 0.7,
    ) -> str:
        logger.info(f"[MockLLMClient] Generating simulated response for role: {role.value}")

        if role == AgentRole.MODERATOR:
            return (
                "Welcome, members of the Boardroom. Today we convene to deliberate upon the submitted proposition. "
                "The objective is rigorous strategic scrutiny. The Supporting Agent will articulate the affirmative "
                "case, highlighting value drivers, empirical feasibility, and strategic upside. The Opposing Agent "
                "will challenge premises, identify systemic liabilities, and provide critical counterpoints. "
                "All members are expected to distinguish verified assumptions from hypotheses and engage directly "
                "with peer arguments. The boardroom is now officially in session."
            )

        elif role == AgentRole.SUPPORTING:
            if "Respond directly to the opposing agent" in user_prompt:
                return (
                    "In response to the Opposing Agent's critiques: while the concerns regarding operational friction "
                    "and upfront resource allocation are valid, they overestimate risk while discounting mitigation pathways. "
                    "First, phased rollout and rigorous milestone gating directly insulate core operations from systemic disruption. "
                    "Second, the cost of inaction or maintaining legacy workflows creates compounding technical and strategic debt. "
                    "Our proposition does not assume zero friction; rather, it provides a bounded, high-ROI transition that "
                    "positions the organization for durable long-term resilience."
                )
            return (
                "We strongly advocate for advancing this proposition. First, from an operational and strategic standpoint, "
                "it addresses critical inefficiencies and unlocks high-leverage organizational value. "
                "Second, empirical industry benchmarks demonstrate that proactive adoption yields sustainable advantages "
                "over conservative latency. While we acknowledge that execution necessitates disciplined governance and "
                "upfront investment, the projected upside and risk-adjusted returns make this the soundest strategic course."
            )

        elif role == AgentRole.OPPOSING:
            if "Address the supporting agent's defense" in user_prompt:
                return (
                    "The Supporting Agent's proposed mitigation—namely phased rollouts and milestone gating—remains largely conceptual. "
                    "Phased rollouts still incur severe parallel operational overhead, dual-system maintenance costs, and team fatigue. "
                    "Furthermore, the underlying assumptions regarding market stability and adoption speed remain unvalidated. "
                    "Until explicit empirical thresholds and downside-capping mechanisms are established, proceeding exposes "
                    "the organization to disproportionate downside risk."
                )
            return (
                "I must challenge the affirmative case on several critical fronts. The Supporting Agent relies on an overly optimistic "
                "assumption of smooth execution, underestimating organizational resistance, integration hurdles, and hidden capital costs. "
                "Historical precedents demonstrate that premature scaling of such initiatives frequently results in sunk capital "
                "and diversion of core focus. A far more prudent alternative is targeted, low-cost experimentation before committing "
                "enterprise resources to an unproven trajectory."
            )

        return "Argument recorded."

    def generate_structured_verdict(
        self,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int = 1000,
    ) -> VerdictStructuredOutput:
        logger.info("[MockLLMClient] Generating simulated structured verdict")
        return VerdictStructuredOutput(
            strongest_supporting=(
                "Proactive adoption mitigates compounding legacy risk and unlocks high-leverage strategic value, "
                "with phased rollout serving as a viable risk containment mechanism."
            ),
            supporting_weaknesses=(
                "Underestimates the high operational friction, parallel maintenance costs, and unvalidated market assumptions."
            ),
            strongest_opposing=(
                "Highlighted that premature enterprise commitment without empirical downside-capping risks sunk capital "
                "and significant operational distraction."
            ),
            opposing_weaknesses=(
                "Overly conservative bias toward status-quo inertia, failing to propose a concrete path to capture the strategic upside."
            ),
            reasoning=(
                "Both agents demonstrated strong intellectual rigor. The Supporting Agent established clear strategic necessity, "
                "while the Opposing Agent effectively exposed the lack of concrete downside containment. The pivotal trade-off "
                "centers on whether the organization possesses the governance maturity to execute phased milestones without cost bloat."
            ),
            final_assessment=(
                "CONDITIONAL APPROVAL: Proceed with a strictly bounded pilot initiative capped at 15% budget allocation. "
                "Transition to full rollout only upon meeting pre-defined operational performance metrics."
            ),
        )


class OpenAILLMClient(BaseLLMClient):
    """
    Robust OpenAI-compatible client with bounded retries, exponential backoff, jitter,
    and schema validation.
    """

    def __init__(self):
        api_key = settings.OPENAI_API_KEY
        if not api_key or api_key == "your_openai_api_key_here":
            raise LLMAuthenticationError(
                "OPENAI_API_KEY is not configured in .env or environment variables."
            )

        client_kwargs = {
            "api_key": api_key,
            "timeout": settings.LLM_REQUEST_TIMEOUT_SECONDS,
        }
        if settings.OPENAI_BASE_URL:
            client_kwargs["base_url"] = settings.OPENAI_BASE_URL

        self.client = OpenAI(**client_kwargs)
        self.model = settings.OPENAI_MODEL

    def _execute_with_retry(self, fn):
        retries = settings.LLM_MAX_RETRIES
        delay = settings.LLM_BACKOFF_FACTOR

        for attempt in range(1, retries + 1):
            try:
                return fn()
            except AuthenticationError as e:
                # Do NOT retry auth errors and do not expose internal key in message
                logger.error("Permanent LLM authentication error: Authentication failed with provider.")
                raise LLMAuthenticationError("Authentication failed: invalid or unauthorized API credentials.") from None
            except APITimeoutError as e:
                logger.warning(f"LLM request timed out (attempt {attempt}/{retries}).")
                if attempt == retries:
                    raise LLMTimeoutError(f"Request timed out after {retries} attempts.") from None
            except APIError as e:
                logger.warning(f"LLM API error (attempt {attempt}/{retries}): status_code={getattr(e, 'status_code', 'unknown')}")
                if attempt == retries:
                    raise LLMProviderError(f"LLM API provider error: HTTP {getattr(e, 'status_code', 'error')}") from None
            except LLMOutputParseError:
                # Do not retry deterministic schema parse failures
                raise
            except Exception as e:
                logger.warning(f"Unexpected error during LLM call (attempt {attempt}/{retries}): {type(e).__name__}")
                if attempt == retries:
                    raise LLMProviderError(f"LLM call failed due to {type(e).__name__}.") from None

            # Jittered exponential backoff
            sleep_time = (delay * (2 ** (attempt - 1))) + random.uniform(0.1, 0.5)
            time.sleep(sleep_time)

    def generate(
        self,
        system_prompt: str,
        user_prompt: str,
        role: AgentRole,
        max_tokens: int = 600,
        temperature: float = 0.7,
    ) -> str:
        def _call():
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                max_tokens=max_tokens,
                temperature=temperature,
            )
            content = response.choices[0].message.content
            if not content:
                raise LLMProviderError("Received empty response from LLM.")
            return content.strip()

        return self._execute_with_retry(_call)

    def generate_structured_verdict(
        self,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int = 1000,
    ) -> VerdictStructuredOutput:
        def _call():
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                max_tokens=max_tokens,
                temperature=0.3,
                response_format={"type": "json_object"},
            )
            raw = response.choices[0].message.content
            if not raw:
                raise LLMOutputParseError("Empty response returned for judge verdict.")

            try:
                # Clean any lingering code fences
                cleaned = raw.strip()
                if cleaned.startswith("```json"):
                    cleaned = cleaned[7:]
                if cleaned.startswith("```"):
                    cleaned = cleaned[3:]
                if cleaned.endswith("```"):
                    cleaned = cleaned[:-3]
                cleaned = cleaned.strip()

                data = json.loads(cleaned)
                return VerdictStructuredOutput(**data)
            except Exception as e:
                logger.error(f"Failed to parse verdict JSON: {e}. Raw content: {raw}")
                raise LLMOutputParseError(f"Failed to parse judge verdict schema: {e}") from e

        return self._execute_with_retry(_call)


def get_llm_client() -> BaseLLMClient:
    """Factory to retrieve configured LLM client."""
    provider = settings.LLM_PROVIDER.lower()
    if provider == "openai":
        return OpenAILLMClient()
    return MockLLMClient()
