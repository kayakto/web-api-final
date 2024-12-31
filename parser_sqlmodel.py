import asyncio
from typing import Optional, List
from pydantic import BaseModel
from fastapi import FastAPI, Depends, HTTPException, WebSocket
from sqlmodel import Field, SQLModel, select
from sqlalchemy.ext.asyncio import (AsyncSession,
                                    create_async_engine, async_sessionmaker)
from starlette.websockets import WebSocketDisconnect
import json

app = FastAPI()

sqlite_file_name = "prices.db"
sqlite_url = f"sqlite+aiosqlite:///{sqlite_file_name}"

engine = create_async_engine(sqlite_url, echo=False)
async_session = async_sessionmaker(engine, class_=AsyncSession,
                                   expire_on_commit=False)


class ConnectionManager:

    def __init__(self):
        self.connections: list[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.connections.append(websocket)

    async def disconnect(self, websocket: WebSocket):
        self.connections.remove(websocket)

    async def broadcast(self, data: str):
        for conn in self.connections:
            try:
                await conn.send_text(data)
            except Exception as e:
                print(f"Ошибка отправки данных клиенту: {e}")
                self.connections.remove(conn)


manager = ConnectionManager()


async def notify_via_websocket(method: str, data: dict, message: str):
    notification = {
        "method": method,
        "data": data,
        "message": message
    }
    await manager.broadcast(json.dumps(notification, ensure_ascii=False))


class Prices(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    price: int


class ItemCreate(BaseModel):
    name: str
    price: int


async def create_db_and_tables():
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)


base_url = "https://www.maxidom.ru/"
start_path = "catalog/kruzhki/"


async def background_parser_async():
    """
    Фоновая задача для парсинга данных и сохранения их в базе данных.
    """
    from parser import find_name_and_price

    while True:
        try:
            print("Начало парсинга...")
            await manager.broadcast(data="Начало парсинга...")
            elements = await asyncio.to_thread(find_name_and_price, base_url,
                                               start_path)
            count_dublicates = 0
            if elements:
                async with async_session() as session:
                    for name, price in elements:
                        query = select(Prices).where(Prices.name == name,
                                                     Prices.price == price)
                        result = await session.execute(query)
                        product_exists = result.scalar_one_or_none()
                        if not product_exists:
                            item = Prices(name=name, price=price)
                            session.add(item)
                        else:
                            count_dublicates += 1
                            print(f"Пропускаем дубликат {name} - {price}")
                    await session.commit()
                message = f"Добавлено {len(elements)-count_dublicates} элементов в базу данных."
                print(message)
                await manager.broadcast(message)
            else:
                message = "Элементы не найдены"
                print(message)
                await manager.broadcast(message)
        except Exception as e:
            error_message = f"Ошибка при парсинге: {e}"
            print(error_message)
            await manager.broadcast(error_message)
        await asyncio.sleep(12 * 60 * 60)  # 12 часов


async def get_session() -> AsyncSession:
    async with async_session() as session:
        yield session


@app.on_event("startup")
async def startup_event():
    """
    Событие, выполняемое при старте приложения.
    Запускает фоновую задачу для парсинга данных.
    """
    await create_db_and_tables()
    asyncio.create_task(background_parser_async())


@app.get("/prices/", response_model=List[Prices])
async def read_prices(session: AsyncSession = Depends(get_session)):
    stmt = select(Prices)
    result = await session.execute(stmt)
    return result.scalars().all()


@app.get("/prices-with-offset/", response_model=List[Prices])
async def read_prices_offset_limit(offset: int, limit: int, session: AsyncSession = Depends(get_session)):
    stmt = select(Prices).offset(offset).limit(limit)
    result = await session.execute(stmt)
    return result.scalars().all()


@app.get("/prices/{item_id}", response_model=Prices)
async def read_item(item_id: int, session: AsyncSession = Depends(get_session)):
    item = await session.get(Prices, item_id)
    if item:
        return item
    raise HTTPException(
        status_code=404,
        detail="Такого элемента не существует. Кружка не найдена."
    )


@app.put("/prices/{item_id}", response_model=Prices)
async def update_item(item_id: int, new_item: ItemCreate,
                      session: AsyncSession = Depends(get_session)):
    db_item = await session.get(Prices, item_id)
    if db_item:
        db_item.name = new_item.name
        db_item.price = new_item.price
        await session.commit()
        await session.refresh(db_item)
        await notify_via_websocket(
            method="update",
            data=db_item.model_dump(),
            message="Item updated successfully")
        return db_item
    raise HTTPException(
        status_code=404,
        detail="Такого элемента не существует. Кружка не найдена."
    )


@app.post("/prices/create", response_model=Prices)
async def create_item(new_item: ItemCreate,
                      session: AsyncSession = Depends(get_session)):
    item = Prices(name=new_item.name, price=new_item.price)
    session.add(item)
    await session.commit()
    await session.refresh(item)

    await notify_via_websocket(
            method="create",
            data=item.model_dump(),
            message="Item created successfully")

    return item


@app.delete("/prices/{item_id}", response_model=dict)
async def delete_item(item_id: int,
                      session: AsyncSession = Depends(get_session)):
    db_item = await session.get(Prices, item_id)
    if db_item:
        await session.delete(db_item)
        await session.commit()

        await notify_via_websocket(
            method="delete", 
            data=db_item.model_dump(),
            message="Item deleted successfully")

        return {"status": "ok"}
    raise HTTPException(status_code=404, detail="Такого элемента не существует. Кружка не найдена.")


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket,
                             session: AsyncSession = Depends(get_session)):
    await manager.connect(websocket)
    try:
        while True:
            data = await websocket.receive_text()
            print(f"Received: {data}")
            if data == "get_price":
                # Получаем список цен из базы данных
                prices = await read_prices(session)
                # Преобразуем данные в JSON-формат
                response_data = [
                    {"id": price.id, "name": price.name, "price": price.price}
                    for price in prices
                ]

                # Отправляем данные клиенту в формате JSON
                await websocket.send_text(json.dumps({
                    "method": "get_price",
                    "data": response_data,
                    "message": "Prices fetched successfully."
                }, ensure_ascii=False))
                continue
            # Если получено что-то другое, отправляем
            await websocket.send_text(json.dumps({
                "method": "unknown_command",
                "data": None,
                "message": f"Unknown command: {data}"
            }, ensure_ascii=False))
    except WebSocketDisconnect:
        print(f"Client {websocket.client} disconnected")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
