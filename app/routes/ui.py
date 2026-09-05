from fastapi import APIRouter

from app.ui.routes import mutations, pages, reads


router = APIRouter()
router.include_router(pages.router)
router.include_router(reads.router)
router.include_router(mutations.router)
