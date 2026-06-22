"""PromptVariant DB access — 給 AgentService 取 active prompt 用"""
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from recommender.models.prompt_variant import PromptVariant


class PromptVariantRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list_active(self, name: str) -> list[PromptVariant]:
        """取出某 name 下所有 is_active=True 的 variants(供 A/B 選擇)"""
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
