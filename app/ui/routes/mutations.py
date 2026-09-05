from fastapi import APIRouter

from app.ui.routes import (
    backup_mutations,
    hardening_mutations,
    input_mutations,
    output_mutations,
    settings_mutations,
)


router = APIRouter()
router.include_router(hardening_mutations.router)
router.include_router(backup_mutations.router)
router.include_router(input_mutations.router)
router.include_router(output_mutations.router)
router.include_router(settings_mutations.router)
