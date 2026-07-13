from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter()

class Item(BaseModel):
    name: str
    description: str | None = None

@router.get("/items")
async def get_items():
    """Get all items"""
    return {"items": []}

@router.post("/items")
async def create_item(item: Item):
    """Create a new item"""
    return {"item": item}

@router.get("/items/{item_id}")
async def get_item(item_id: int):
    """Get a specific item"""
    return {"item_id": item_id}
