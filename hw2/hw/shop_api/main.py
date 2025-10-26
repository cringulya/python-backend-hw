import os
from http import HTTPStatus
from typing import Any, Optional

from fastapi import FastAPI, Query, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict
from prometheus_fastapi_instrumentator import Instrumentator
from sqlalchemy import (
    create_engine,
    Column,
    Integer,
    String,
    Float,
    Boolean,
    ForeignKey,
    Table,
)
from sqlalchemy.orm import declarative_base
from sqlalchemy.orm import sessionmaker, Session, relationship

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./test.db")

# Configure connection args based on database type
connect_args = {}
if DATABASE_URL.startswith("sqlite"):
    connect_args = {"check_same_thread": False}

engine = create_engine(DATABASE_URL, connect_args=connect_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


cart_items = Table(
    "cart_items",
    Base.metadata,
    Column("cart_id", Integer, ForeignKey("carts.id"), primary_key=True),
    Column("item_id", Integer, ForeignKey("items.id"), primary_key=True),
    Column("quantity", Integer, default=1),
)


class Item(Base):
    __tablename__ = "items"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    price = Column(Float, nullable=False)
    deleted = Column(Boolean, default=False)

    def serialize(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "price": self.price,
            "deleted": self.deleted,
        }


class Cart(Base):
    __tablename__ = "carts"

    id = Column(Integer, primary_key=True, index=True)
    items = relationship("Item", secondary=cart_items, backref="carts")

    def add_item(self, db: Session, item_id: int) -> None:
        result = db.execute(
            cart_items.select().where(
                cart_items.c.cart_id == self.id, cart_items.c.item_id == item_id
            )
        ).first()

        if result:
            db.execute(
                cart_items.update()
                .where(
                    cart_items.c.cart_id == self.id,
                    cart_items.c.item_id == item_id,
                )
                .values(quantity=result.quantity + 1)
            )
        else:
            db.execute(
                cart_items.insert().values(
                    cart_id=self.id, item_id=item_id, quantity=1
                )
            )
        db.commit()

    def quantity(self, db: Session) -> int:
        result = db.execute(
            cart_items.select().where(cart_items.c.cart_id == self.id)
        ).fetchall()

        total = 0
        for row in result:
            item = db.query(Item).filter(Item.id == row.item_id).first()
            if item and not item.deleted:
                total += row.quantity
        return total

    def price(self, db: Session) -> float:
        result = db.execute(
            cart_items.select().where(cart_items.c.cart_id == self.id)
        ).fetchall()

        total = 0.0
        for row in result:
            item = db.query(Item).filter(Item.id == row.item_id).first()
            if item and not item.deleted:
                total += row.quantity * item.price
        return total

    def serialize(self, db: Session) -> dict[str, Any]:
        result = db.execute(
            cart_items.select().where(cart_items.c.cart_id == self.id)
        ).fetchall()

        items_list = []
        for row in result:
            item = db.query(Item).filter(Item.id == row.item_id).first()
            if item:
                items_list.append(
                    {
                        "id": item.id,
                        "name": item.name,
                        "quantity": row.quantity,
                        "available": not item.deleted,
                    }
                )

        return {
            "id": self.id,
            "price": self.price(db),
            "items": items_list,
        }


Base.metadata.create_all(bind=engine)

app = FastAPI(title="Shop API")
Instrumentator().instrument(app).expose(app)


@app.on_event("startup")
async def startup_event():
    Base.metadata.create_all(bind=engine)
    print("Database tables created successfully!")


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@app.post("/cart")
def create_cart(db: Session = Depends(get_db)):
    cart = Cart()
    db.add(cart)
    db.commit()
    db.refresh(cart)
    return JSONResponse(
        status_code=HTTPStatus.CREATED,
        content={"id": cart.id},
        headers={"Location": f"/cart/{cart.id}"},
    )


@app.get("/cart/{id}")
def get_cart_by_id(id: int, db: Session = Depends(get_db)):
    cart = db.query(Cart).filter(Cart.id == id).first()
    if cart:
        return cart.serialize(db)

    return JSONResponse(
        status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
        content={"message": "no such id"},
    )


@app.get("/cart")
def get_cart_with_params(
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=10, ge=1),
    min_price: Optional[float] = Query(default=None, ge=0),
    max_price: Optional[float] = Query(default=None, ge=0),
    min_quantity: Optional[int] = Query(default=None, ge=0),
    max_quantity: Optional[int] = Query(default=None, ge=0),
    db: Session = Depends(get_db),
):
    carts = db.query(Cart).offset(offset).limit(limit).all()

    filtered_carts = []
    for cart in carts:
        price = cart.price(db)
        quantity = cart.quantity(db)

        if (
            (min_price is None or price >= min_price)
            and (max_price is None or price <= max_price)
            and (min_quantity is None or quantity >= min_quantity)
            and (max_quantity is None or quantity <= max_quantity)
        ):
            filtered_carts.append(cart.serialize(db))

    return filtered_carts


@app.post("/cart/{cart_id}/add/{item_id}")
def post_cart(cart_id: int, item_id: int, db: Session = Depends(get_db)):
    cart = db.query(Cart).filter(Cart.id == cart_id).first()
    if not cart:
        return JSONResponse(
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            content={"message": "no such cart_id"},
        )

    item = db.query(Item).filter(Item.id == item_id).first()
    if not item:
        return JSONResponse(
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            content={"message": "no such item_id"},
        )

    cart.add_item(db, item_id)
    return JSONResponse(
        status_code=HTTPStatus.OK, content={"message": "item added"}
    )


class ItemCreate(BaseModel):
    name: str
    price: float


@app.post("/item")
def add_item(item: ItemCreate, db: Session = Depends(get_db)):
    db_item = Item(name=item.name, price=item.price)
    db.add(db_item)
    db.commit()
    db.refresh(db_item)
    return JSONResponse(
        status_code=HTTPStatus.CREATED, content=db_item.serialize()
    )


@app.get("/item/{id}")
def get_item_by_id(id: int, db: Session = Depends(get_db)):
    item = db.query(Item).filter(Item.id == id, Item.deleted == False).first()
    if item:
        return item.serialize()
    return JSONResponse(
        status_code=HTTPStatus.NOT_FOUND, content={"message": "not found"}
    )


@app.get("/item")
def get_item_query(
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=10, ge=1),
    min_price: Optional[float] = Query(default=None, ge=0),
    max_price: Optional[float] = Query(default=None, ge=0),
    show_deleted: bool = False,
    db: Session = Depends(get_db),
):
    query = db.query(Item)

    if not show_deleted:
        query = query.filter(Item.deleted == False)

    if min_price is not None:
        query = query.filter(Item.price >= min_price)

    if max_price is not None:
        query = query.filter(Item.price <= max_price)

    items = query.offset(offset).limit(limit).all()
    return [item.serialize() for item in items]


@app.put("/item/{id}")
def put_item(id: int, item: ItemCreate, db: Session = Depends(get_db)):
    db_item = db.query(Item).filter(Item.id == id).first()

    if not db_item:
        db_item = Item(id=id, name=item.name, price=item.price)
        db.add(db_item)
    else:
        db_item.name = item.name
        db_item.price = item.price

    db.commit()
    db.refresh(db_item)
    return JSONResponse(status_code=HTTPStatus.OK, content=db_item.serialize())


class ItemUpdate(BaseModel):
    name: Optional[str] = None
    price: Optional[float] = None
    model_config = ConfigDict(extra="forbid")


@app.patch("/item/{id}")
def patch_item(id: int, updates: ItemUpdate, db: Session = Depends(get_db)):
    item = db.query(Item).filter(Item.id == id).first()

    if not item:
        return JSONResponse(
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            content={"message": "not found"},
        )

    if item.deleted:
        return JSONResponse(
            status_code=HTTPStatus.NOT_MODIFIED,
            content={"message": "not modified"},
        )

    if updates.name is not None:
        item.name = updates.name
    if updates.price is not None:
        item.price = updates.price

    db.commit()
    db.refresh(item)
    return item.serialize()


@app.delete("/item/{id}")
def delete_item(id: int, db: Session = Depends(get_db)):
    item = db.query(Item).filter(Item.id == id).first()
    if item:
        item.deleted = True
        db.commit()
        return JSONResponse(
            status_code=HTTPStatus.OK, content={"message": "item deleted"}
        )
    return JSONResponse(
        status_code=HTTPStatus.NOT_FOUND, content={"message": "not found"}
    )
