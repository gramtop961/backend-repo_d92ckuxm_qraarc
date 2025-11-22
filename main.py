import os
from fastapi import FastAPI, HTTPException, Body
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from typing import List, Optional
from bson import ObjectId

from database import db, create_document, get_documents
from schemas import Product as ProductSchema, Order as OrderSchema, OrderItem as OrderItemSchema, CustomerInfo

app = FastAPI(title="Vistro API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Helpers
class ProductCreate(ProductSchema):
    pass

class ProductResponse(ProductSchema):
    id: str

class ProductsListResponse(BaseModel):
    items: List[ProductResponse]

class OrderCreate(OrderSchema):
    pass

class OrderResponse(OrderSchema):
    id: str

class PaymentRequest(BaseModel):
    order_id: str = Field(...)
    card_number: str = Field(..., min_length=12, max_length=19)
    exp_month: int = Field(..., ge=1, le=12)
    exp_year: int = Field(..., ge=2024, le=2100)
    cvc: str = Field(..., min_length=3, max_length=4)
    name_on_card: Optional[str] = None


def _serialize(doc: dict) -> dict:
    d = {**doc}
    if "_id" in d:
        d["id"] = str(d.pop("_id"))
    # Convert nested ObjectIds if any
    return d


def _luhn_valid(card_number: str) -> bool:
    digits = [int(ch) for ch in card_number if ch.isdigit()]
    if len(digits) < 12:
        return False
    checksum = 0
    parity = len(digits) % 2
    for i, d in enumerate(digits):
        if i % 2 == parity:
            d *= 2
            if d > 9:
                d -= 9
        checksum += d
    return checksum % 10 == 0


def _brand_from_number(num: str) -> str:
    n = num.replace(" ", "")
    if n.startswith("4"):
        return "visa"
    if n[:2] in {"51","52","53","54","55"} or (2221 <= int(n[:4] or 0) <= 2720):
        return "mastercard"
    if n.startswith("34") or n.startswith("37"):
        return "amex"
    return "card"


@app.get("/")
def read_root():
    return {"message": "Vistro backend running"}


@app.get("/api/products", response_model=ProductsListResponse)
def list_products():
    docs = get_documents("product")
    items = [ProductResponse(**_serialize(d)) for d in docs]
    return {"items": items}


@app.post("/api/products", response_model=ProductResponse)
def create_product(payload: ProductCreate):
    product_id = create_document("product", payload)
    doc = db["product"].find_one({"_id": ObjectId(product_id)})
    return ProductResponse(**_serialize(doc))


@app.get("/api/products/{product_id}", response_model=ProductResponse)
def get_product(product_id: str):
    try:
        doc = db["product"].find_one({"_id": ObjectId(product_id)})
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid product id")
    if not doc:
        raise HTTPException(status_code=404, detail="Product not found")
    return ProductResponse(**_serialize(doc))


@app.post("/api/orders", response_model=OrderResponse)
def create_order(order: OrderCreate):
    # Basic totals validation
    calc_subtotal = 0.0
    for item in order.items:
        try:
            pdoc = db["product"].find_one({"_id": ObjectId(item.product_id)})
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid product id in items")
        if not pdoc:
            raise HTTPException(status_code=400, detail="Product not found in items")
        calc_subtotal += float(pdoc.get("price", 0)) * int(item.quantity)
    # Allow slight rounding difference
    if abs(calc_subtotal - float(order.subtotal)) > 0.01:
        raise HTTPException(status_code=400, detail="Subtotal mismatch")
    if abs((order.subtotal + order.shipping) - order.total) > 0.01:
        raise HTTPException(status_code=400, detail="Total mismatch")

    order_id = create_document("order", order)
    doc = db["order"].find_one({"_id": ObjectId(order_id)})
    return OrderResponse(**_serialize(doc))


@app.post("/api/payments/charge")
def charge_card(req: PaymentRequest):
    # Validate order exists and pending
    try:
        oid = ObjectId(req.order_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid order id")

    order = db["order"].find_one({"_id": oid})
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    if order.get("status") != "pending":
        raise HTTPException(status_code=400, detail="Order is not payable")

    if not _luhn_valid(req.card_number):
        raise HTTPException(status_code=400, detail="Card was declined")

    last4 = ''.join([ch for ch in req.card_number if ch.isdigit()])[-4:]
    brand = _brand_from_number(req.card_number)

    db["order"].update_one({"_id": oid}, {"$set": {
        "status": "paid",
        "payment_brand": brand,
        "payment_last4": last4,
    }})

    return {"status": "succeeded", "brand": brand, "last4": last4}


@app.get("/api/orders")
def list_orders(limit: int = 50):
    docs = db["order"].find({}).sort("created_at", -1).limit(limit)
    return [{**_serialize(d)} for d in docs]


@app.get("/test")
def test_database():
    response = {
        "backend": "✅ Running",
        "database": "❌ Not Available",
        "database_url": None,
        "database_name": None,
        "connection_status": "Not Connected",
        "collections": []
    }
    try:
        if db is not None:
            response["database"] = "✅ Available"
            response["database_url"] = "✅ Set" if os.getenv("DATABASE_URL") else "❌ Not Set"
            response["database_name"] = "✅ Set" if os.getenv("DATABASE_NAME") else "❌ Not Set"
            response["connection_status"] = "Connected"
            try:
                collections = db.list_collection_names()
                response["collections"] = collections[:10]
                response["database"] = "✅ Connected & Working"
            except Exception as e:
                response["database"] = f"⚠️ Connected but Error: {str(e)[:50]}"
        else:
            response["database"] = "⚠️ Available but not initialized"
    except Exception as e:
        response["database"] = f"❌ Error: {str(e)[:50]}"
    return response


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
