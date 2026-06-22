"""PromptVariant DB access — for AgentService to fetch the active prompt."""
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from recommender.models.prompt_variant import PromptVariant


class PromptVariantRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list_active(self, name: str) -> list[PromptVariant]:
        """Fetch all is_active=True variants under a given name (for A/B selection)."""
        result = await self.session.exec(
            select(PromptVariant)
            .where(PromptVariant.name == name)
            .where(PromptVariant.is_active == True)  # noqa: E712
        )
        return list(result.all())

    async def get(self, variant_id: int) -> PromptVariant | None:
        result = await self.session.exec(
            select(PromptVariant).where(PromptVariant.id == variant_id)
        )
        return result.first()

    async def create(
        self,
        *,
        name: str,
        version: str,
        template: str,
        is_active: bool = False,
        weight: float = 1.0,
        notes: str | None = None,
    ) -> PromptVariant:
        variant = PromptVariant(
            name=name,
            version=version,
            template=template,
            is_active=is_active,
            weight=weight,
            notes=notes,
        )
        self.session.add(variant)
        await self.session.commit()
        await self.session.refresh(variant)
        return variant
