from fastapi import FastAPI, Depends, HTTPException, status, UploadFile, File
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from pydantic import BaseModel
import uuid
import logging
import json
import time
import urllib.request
import models, schemas, auth, database, ai_engine, wellness_utils, gemini_utils
from typing import List, Optional
import csv
import io
from fastapi.responses import StreamingResponse
import math
from datetime import datetime, timedelta
import secrets
from receipt_generator import generate_receipt_pdf
from pesapal_utils import PesapalAPI

logger = logging.getLogger(__name__)

pesapal = PesapalAPI()
gemini_advisor = gemini_utils.GeminiAdvisor()

def calculate_distance(lat1, lon1, lat2, lon2):
    # Haversine formula
    R = 6371  # Earth radius in kilometers
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) * math.sin(dlat / 2) +
         math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) *
         math.sin(dlon / 2) * math.sin(dlon / 2))
    c = 2 * math.asin(math.sqrt(a))
    return R * c

def award_karma(db: Session, user_id: str, amount: int, category: str, description: str = None):
    """Helper to award karma and track the transaction"""
    user = db.query(models.User).filter(models.User.id == user_id).first()
    if user:
        user.karma_points += amount
        user.available_karma += amount
        transaction = models.KarmaTransaction(
            id=str(uuid.uuid4()),
            user_id=user_id,
            amount=amount,
            category=category,
            description=description
        )
        db.add(transaction)
        db.commit()


app = FastAPI(title="Lovedogs 360 API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://lovedogs360.com",
        "https://admin.lovedogs360.com",
        "http://localhost:3000",
        "http://localhost:8081",
        "http://localhost:19006",
        "http://localhost:8082",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token")

# Initialize AI engine
ai_engine_instance = ai_engine.DogIDEngine()

@app.post("/register", response_model=schemas.UserResponse)
def register(user: schemas.UserCreate, db: Session = Depends(database.get_db)):
    db_user = db.query(models.User).filter(models.User.email == user.email).first()
    if db_user:
        raise HTTPException(status_code=400, detail="Email already registered")
    
    hashed_password = auth.get_password_hash(user.password)
    new_user = models.User(
        id=str(uuid.uuid4()),
        email=user.email,
        full_name=user.full_name,
        hashed_password=hashed_password,
        role=user.role,
        phone_number=user.phone_number,
        country=user.country,
        language=user.language,
        latitude=user.latitude,
        longitude=user.longitude,
        address=user.address,
        bio=user.bio
    )
    try:
        db.add(new_user)
        db.commit()
        db.refresh(new_user)
        logger.info(f"Successfully registered user {new_user.email}")
        return new_user
    except Exception as e:
        db.rollback()
        logger.error(f"Error registering user {user.email}: {e}")
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/token", response_model=schemas.Token)
def login(form_data: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(database.get_db)):
    user = db.query(models.User).filter(models.User.email == form_data.username).first()
    if not user or not auth.verify_password(form_data.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    access_token = auth.create_access_token(data={"sub": user.email, "role": user.role})
    return {"access_token": access_token, "token_type": "bearer"}

@app.post("/auth/google", response_model=schemas.GoogleLoginResponse)
async def google_auth(request: schemas.GoogleLoginRequest, db: Session = Depends(database.get_db)):
    # Verify the Google token
    id_info = auth.verify_google_token(request.id_token)
    if not id_info:
        raise HTTPException(status_code=400, detail="Invalid Google token")
    
    email = id_info.get("email")
    full_name = id_info.get("name", "")
    
    # Check if user exists
    user = db.query(models.User).filter(models.User.email == email).first()
    
    if not user:
        # Create new user if they don't exist
        user = models.User(
            id=str(uuid.uuid4()),
            email=email,
            full_name=full_name,
            hashed_password="", # No password for Google users
            role=models.UserRole.BUYER # Default role
        )
        db.add(user)
        db.commit()
        db.refresh(user)
    
    # Create internal access token
    access_token = auth.create_access_token(data={"sub": user.email, "role": user.role})
    
    return {
        "access_token": access_token,
        "token_type": "bearer",
        "user": user
    }

async def get_current_user(token: str = Depends(oauth2_scheme), db: Session = Depends(database.get_db)):
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = auth.jwt.decode(token, auth.SECRET_KEY, algorithms=[auth.ALGORITHM])
        email: str = payload.get("sub")
        if email is None:
            raise credentials_exception
    except auth.JWTError:
        raise credentials_exception
    user = db.query(models.User).filter(models.User.email == email).first()
    if user is None:
        raise credentials_exception
    if user is None:
        raise credentials_exception
    return user

@app.get("/users/me", response_model=schemas.UserResponse)
async def read_users_me(current_user: models.User = Depends(get_current_user)):
    avg_rating = 0.0
    total = len(current_user.ratings_received)
    if total > 0:
        avg_rating = sum(r.score for r in current_user.ratings_received) / total
    
    # Create a response object with computed fields
    response = schemas.UserResponse.from_orm(current_user)
    response.average_rating = avg_rating
    response.total_ratings = total
    return response

@app.put("/users/me", response_model=schemas.UserResponse)
async def update_user_me(
    user_update: schemas.UserUpdate,
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(get_current_user)
):
    user = db.query(models.User).filter(models.User.id == current_user.id).first()
    if user_update.full_name is not None:
        user.full_name = user_update.full_name
    if user_update.phone_number is not None:
        user.phone_number = user_update.phone_number
    if user_update.bio is not None:
        user.bio = user_update.bio
    if user_update.profile_image is not None:
        user.profile_image = user_update.profile_image
    if user_update.country is not None:
        user.country = user_update.country
    if user_update.language is not None:
        user.language = user_update.language
    if user_update.mpesa_phone_number is not None:
        user.mpesa_phone_number = user_update.mpesa_phone_number
    if user_update.preferred_currency is not None:
        user.preferred_currency = user_update.preferred_currency
    
    db.commit()
    db.refresh(user)
    return user

@app.put("/dogs/{dog_id}", response_model=schemas.DogResponse)
async def update_dog(
    dog_id: str,
    dog_update: schemas.DogUpdate,
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(get_current_user)
):
    dog = db.query(models.Dog).filter(models.Dog.id == dog_id).first()
    if not dog:
        raise HTTPException(status_code=404, detail="Dog not found")
    if dog.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not authorized")

    if dog_update.name is not None: dog.name = dog_update.name
    if dog_update.breed is not None: dog.breed = dog_update.breed
    if dog_update.color is not None: dog.color = dog_update.color
    if dog_update.height is not None: dog.height = dog_update.height
    if dog_update.weight is not None: dog.weight = dog_update.weight
    if dog_update.body_structure is not None: dog.body_structure = dog_update.body_structure
    if dog_update.bio is not None: dog.bio = dog_update.bio
    if dog_update.body_image is not None: dog.body_image = dog_update.body_image
    
    db.commit()
    db.refresh(dog)
    return dog

@app.post("/dogs", response_model=schemas.DogResponse)
async def create_dog(
    dog: schemas.DogCreate, 
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(get_current_user)
):
    new_dog = models.Dog(
        id=str(uuid.uuid4()),
        owner_id=current_user.id,
        **dog.dict()
    )
    db.add(new_dog)
    db.commit()
    db.refresh(new_dog)
    return new_dog

@app.post("/dogs/report-lost")
async def report_lost_dog(
    lost_info: schemas.DogCreate,
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(get_current_user)
):
    # Enhanced matching logic: Breed match + Similarity in Color, Age, and Weight
    query = db.query(models.Dog).filter(models.Dog.breed == lost_info.breed)
    
    potential_matches = []
    all_dogs = query.all()
    
    for d in all_dogs:
        score = 0
        if d.color == lost_info.color: score += 50
        
        # Age proximity (within 2 years)
        if d.age and lost_info.age:
            if abs(d.age - lost_info.age) <= 2: score += 25
            
        # Weight proximity (within 5kg)
        if d.weight and lost_info.weight:
            if abs(d.weight - lost_info.weight) <= 5: score += 25
            
        if score >= 50:
            potential_matches.append({"id": d.id, "name": d.name, "similarity": f"{score}%"})
            logger.info(f"Potential match found for dog {d.name} ({score}%). Notifying owner.")

    return {
        "message": f"Lost report processed. Found {len(potential_matches)} potential matches.",
        "matches": potential_matches
    }

@app.post("/dogs/identify")
async def identify_dog(
    file: UploadFile = File(...),
    db: Session = Depends(database.get_db)
):
    contents = await file.read()
    descriptor = ai_engine_instance.extract_descriptor(contents)
    if descriptor is None:
        return {"dog_id": None, "confidence": 0, "message": "Failed to extract nose print"}

    known_dogs = db.query(models.Dog).all()
    match = ai_engine_instance.identify_dog(descriptor, known_dogs)
    
    if match and match["confidence"] > 60: # Threshold
        return {
            "dog_id": match["dog_id"],
            "name": match["name"],
            "confidence": match["confidence"],
            "message": f"Best match: {match['name']}"
        }
    
    return {"dog_id": None, "confidence": match["confidence"] if match else 0, "message": "No strong match found"}

@app.get("/my-dogs", response_model=List[schemas.DogResponse])
def get_my_dogs(
    db: Session = Depends(database.get_db), 
    current_user: models.User = Depends(get_current_user)
):
    return db.query(models.Dog).filter(models.Dog.owner_id == current_user.id).all()

@app.get("/services", response_model=List[schemas.ServiceResponse])
def list_services(
    item_type: Optional[str] = None, 
    lat: Optional[float] = None, 
    lon: Optional[float] = None, 
    radius: Optional[float] = 50.0, # default 50km
    db: Session = Depends(database.get_db)
):
    query = db.query(models.Service).filter(models.Service.is_published == True, models.Service.admin_approved == True)
    if item_type:
        query = query.filter(models.Service.item_type == item_type)
    
    services = query.all()
    
    if lat is not None and lon is not None:
        # Filter and sort by distance
        filtered_services = []
        for s in services:
            if s.latitude is not None and s.longitude is not None:
                dist = calculate_distance(lat, lon, s.latitude, s.longitude)
                if dist <= radius:
                    # Injects distance into the response object temporarily
                    setattr(s, 'distance', round(dist, 2))
                    filtered_services.append(s)
            else:
                # Services without location might be shown at the end or filtered out
                # For "Uber-like" we might prefer showing only ones with location
                pass
        
        # Sort by distance
        filtered_services.sort(key=lambda x: getattr(x, 'distance', 999999))
        return filtered_services
        
    return services

@app.post("/services", response_model=schemas.ServiceResponse)
def create_service(
    service: schemas.ServiceCreate, 
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(get_current_user)
):
    # Apply 23.5% platform fee markup to the provider's price
    final_price = round(service.price * 1.235, 2)

    new_service = models.Service(
        id=str(uuid.uuid4()),
        provider_id=current_user.id,
        title=service.title,
        description=service.description,
        price=final_price,
        category=service.category,
        item_type=service.item_type,
        image_url=service.image_url,
        latitude=service.latitude,
        longitude=service.longitude,
        address=service.address,
        is_published=service.is_published
    )
    db.add(new_service)

    # Handle Nested Form Fields
    if service.form_fields:
        for f_idx, field_data in enumerate(service.form_fields):
            new_field = models.ServiceFormField(
                id=str(uuid.uuid4()),
                service_id=new_service.id,
                field_type=field_data.field_type,
                label=field_data.label,
                options=field_data.options,
                is_required=field_data.is_required,
                sort_order=field_data.sort_order or f_idx
            )
            db.add(new_field)

    db.commit()
    db.refresh(new_service)
    return new_service
    
@app.get("/services/{service_id}", response_model=schemas.ServiceResponse)
def get_service_detail(service_id: str, db: Session = Depends(database.get_db), current_user: models.User = Depends(get_current_user)):
    service = db.query(models.Service).filter(models.Service.id == service_id).first()
    if not service:
        raise HTTPException(status_code=404, detail="Service not found")
    
    # Security: If not approved, only provider or admin can view
    if not service.admin_approved and service.provider_id != current_user.id and current_user.role != models.UserRole.ADMIN:
        raise HTTPException(status_code=403, detail="Service pending admin approval")
        
    return service

@app.get("/services/{service_id}/form-fields", response_model=List[schemas.ServiceFormFieldResponse])
def get_service_form_fields(service_id: str, db: Session = Depends(database.get_db)):
    return db.query(models.ServiceFormField).filter(models.ServiceFormField.service_id == service_id).order_by(models.ServiceFormField.sort_order).all()

@app.post("/services/{service_id}/form-fields")
def save_service_form_fields(
    service_id: str,
    fields: List[schemas.ServiceFormFieldCreate],
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(get_current_user)
):
    service = db.query(models.Service).filter(models.Service.id == service_id).first()
    if not service:
        raise HTTPException(status_code=404, detail="Service not found")
    if service.provider_id != current_user.id and current_user.role != models.UserRole.ADMIN:
        raise HTTPException(status_code=403, detail="Not authorized")

    # Sync fields
    db.query(models.ServiceFormField).filter(models.ServiceFormField.service_id == service_id).delete()
    for f in fields:
        db.add(models.ServiceFormField(
            id=str(uuid.uuid4()), service_id=service_id, field_type=f.field_type,
            label=f.label, options=f.options, is_required=f.is_required, sort_order=f.sort_order
        ))
    db.commit()
    return {"status": "success"}

@app.get("/services/{service_id}/responses")
def get_service_responses(
    service_id: str,
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(get_current_user)
):
    service = db.query(models.Service).filter(models.Service.id == service_id).first()
    if not service:
        raise HTTPException(status_code=404, detail="Service not found")
    if service.provider_id != current_user.id and current_user.role != models.UserRole.ADMIN:
        raise HTTPException(status_code=403, detail="Not authorized")

    orders = db.query(models.Order).filter(models.Order.service_id == service_id).all()
    results = []
    for o in orders:
        buyer = db.query(models.User).filter(models.User.id == o.buyer_id).first()
        resps = []
        for r in o.responses:
            resps.append({
                "id": r.id, "field_id": r.field_id,
                "field_label": r.field.label if r.field else "Deleted Field",
                "answer_value": r.answer_value
            })
        results.append({
            "order_id": o.id, "status": o.status, "created_at": o.created_at,
            "buyer": {"full_name": buyer.full_name, "email": buyer.email, 
                      "phone": buyer.phone_number if (o.share_phone or current_user.role == "admin") else None},
            "responses": resps
        })
    return results

@app.post("/orders", response_model=schemas.OrderResponse)
def create_order(
    order_data: schemas.OrderCreate, 
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(get_current_user)
):
    service = db.query(models.Service).filter(models.Service.id == order_data.service_id).first()
    if not service:
        raise HTTPException(status_code=404, detail="Service not found")
        
    # Validate required form fields
    form_fields = db.query(models.ServiceFormField).filter(models.ServiceFormField.service_id == order_data.service_id).all()
    provided = {r.field_id: r.answer_value for r in (order_data.form_responses or [])}
    for field in form_fields:
        if field.is_required and not provided.get(field.id):
            raise HTTPException(status_code=400, detail=f"Question '{field.label}' is required")

    payout = round(service.price / 1.235, 2)
    commission = round(service.price - payout, 2)
    
    new_order = models.Order(
        id=str(uuid.uuid4()),
        buyer_id=current_user.id,
        service_id=order_data.service_id,
        amount=service.price,
        commission=commission,
        payout=payout,
        status=models.OrderStatus.PENDING,
        share_phone=order_data.share_phone
    )
    db.add(new_order)
    db.flush()
    
    if order_data.form_responses:
        for resp in order_data.form_responses:
            db.add(models.OrderFormResponse(
                id=str(uuid.uuid4()), order_id=new_order.id,
                field_id=resp.field_id, answer_value=resp.answer_value
            ))
            
    db.commit()
    db.refresh(new_order)
    return new_order

@app.post("/orders/{order_id}/pay")
def pay_order(order_id: str, db: Session = Depends(database.get_db), current_user: models.User = Depends(get_current_user)):
    order = db.query(models.Order).filter(models.Order.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    if order.buyer_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    if order.status == models.OrderStatus.PAID:
        return {"message": "Order already paid", "status": order.status}

    # Decrement stock/slots
    service = db.query(models.Service).filter(models.Service.id == order.service_id).first()
    if service:
        if service.item_type == "products":
            if service.stock_count > 0:
                service.stock_count -= 1
        else: # services
            if service.slots_available > 0:
                service.slots_available -= 1

    order.status = models.OrderStatus.PAID
    db.commit()
    return {"message": "Order paid successfully", "status": order.status}

@app.get("/orders/{order_id}/receipt")
def get_order_receipt(order_id: str, db: Session = Depends(database.get_db), current_user: models.User = Depends(get_current_user)):
    order = db.query(models.Order).filter(models.Order.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    
    if order.status != models.OrderStatus.PAID:
        raise HTTPException(status_code=400, detail="Receipt only available for paid orders")

    # Allow buyer OR provider OR admin to get receipt
    service = db.query(models.Service).filter(models.Service.id == order.service_id).first()
    if not service:
         raise HTTPException(status_code=404, detail="Service not found")

    if current_user.id not in [order.buyer_id, service.provider_id] and current_user.role != models.UserRole.ADMIN:
        raise HTTPException(status_code=403, detail="Not authorized")

    buyer = db.query(models.User).filter(models.User.id == order.buyer_id).first()
    provider = db.query(models.User).filter(models.User.id == service.provider_id).first()

    pdf_content = generate_receipt_pdf(order, service, buyer, provider)
    
    return StreamingResponse(
        io.BytesIO(pdf_content),
        media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename=receipt_{order_id}.pdf"}
    )

# --- Buyer Orders & Seller Earnings ---

@app.get("/my-orders")
def get_my_orders(db: Session = Depends(database.get_db), current_user: models.User = Depends(get_current_user)):
    """Buyer sees all their purchases with payment status."""
    orders = db.query(models.Order).filter(models.Order.buyer_id == current_user.id).order_by(models.Order.created_at.desc()).all()
    result = []
    for order in orders:
        service = db.query(models.Service).filter(models.Service.id == order.service_id).first()
        provider = None
        if service:
            provider = db.query(models.User).filter(models.User.id == service.provider_id).first()
        result.append({
            "id": order.id,
            "service_title": service.title if service else "Unknown",
            "service_image": service.image_url if service else None,
            "provider_name": provider.full_name if provider else "Unknown",
            "amount": order.amount,
            "status": order.status,
            "created_at": str(order.created_at) if order.created_at else None
        })
    return result

@app.get("/my-earnings")
def get_my_earnings(db: Session = Depends(database.get_db), current_user: models.User = Depends(get_current_user)):
    """Seller sees all earnings from their services, with escrow/available status."""
    # Get all services owned by the current user
    my_services = db.query(models.Service).filter(models.Service.provider_id == current_user.id).all()
    service_ids = [s.id for s in my_services]

    if not service_ids:
        return {"wallet": {"total_earned": 0, "in_escrow": 0, "available": 0, "settled": 0}, "earnings": []}

    # Get all orders for those services that have been paid or beyond
    paid_statuses = [models.OrderStatus.PAID, models.OrderStatus.COMPLETED, models.OrderStatus.SETTLED]
    orders = db.query(models.Order).filter(
        models.Order.service_id.in_(service_ids),
        models.Order.status.in_(paid_statuses)
    ).order_by(models.Order.created_at.desc()).all()

    earnings = []
    total_earned = 0
    in_escrow = 0
    available = 0
    settled = 0

    for order in orders:
        service = db.query(models.Service).filter(models.Service.id == order.service_id).first()
        buyer = db.query(models.User).filter(models.User.id == order.buyer_id).first()
        payout = order.payout or 0
        status_lower = (order.status or "").lower()

        total_earned += payout

        # Determine escrow state:
        # paid = buyer paid but delivery not confirmed → in escrow (greyed out)
        # completed = delivery confirmed but payout not approved → available (highlighted)
        # settled = payout disbursed → settled (green checkmark)
        if status_lower == "paid":
            in_escrow += payout
            escrow_label = "in_escrow"
        elif status_lower == "completed":
            available += payout
            escrow_label = "available"
        elif status_lower == "settled":
            settled += payout
            escrow_label = "settled"
        else:
            escrow_label = "unknown"

        earnings.append({
            "id": order.id,
            "service_title": service.title if service else "Unknown",
            "buyer_name": buyer.full_name if buyer else "Unknown",
            "gross_amount": order.amount,
            "commission": order.commission,
            "payout": payout,
            "order_status": order.status,
            "escrow_status": escrow_label,
            "created_at": str(order.created_at) if order.created_at else None
        })

    return {
        "wallet": {
            "total_earned": round(total_earned, 2),
            "in_escrow": round(in_escrow, 2),
            "available": round(available, 2),
            "settled": round(settled, 2)
        },
        "earnings": earnings
    }

# --- Ratings API ---

@app.post("/ratings", response_model=schemas.RatingResponse)
def submit_rating(rating: schemas.RatingCreate, db: Session = Depends(database.get_db), current_user: models.User = Depends(get_current_user)):
    # Verify order exists and belongs to rater
    order = db.query(models.Order).filter(models.Order.id == rating.order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    if order.buyer_id != current_user.id:
        raise HTTPException(status_code=403, detail="Only the buyer can rate this service")
    if order.status not in [models.OrderStatus.PAID, models.OrderStatus.COMPLETED]:
        raise HTTPException(status_code=400, detail="Can only rate completed/paid services")
    
    # Check if already rated
    existing_rating = db.query(models.Rating).filter(models.Rating.order_id == rating.order_id).first()
    if existing_rating:
        raise HTTPException(status_code=400, detail="Order already rated")

    new_rating = models.Rating(
        id=str(uuid.uuid4()),
        order_id=rating.order_id,
        rater_id=current_user.id,
        rated_id=rating.rated_id,
        score=rating.score,
        comment=rating.comment
    )
    db.add(new_rating)

    # Update rated user's average_rating and total_ratings
    rated_user = db.query(models.User).filter(models.User.id == rating.rated_id).first()
    if rated_user:
        current_total = rated_user.total_ratings or 0
        current_avg = rated_user.average_rating or 0.0
        
        new_total = current_total + 1
        new_avg = ((current_avg * current_total) + rating.score) / new_total
        
        rated_user.total_ratings = new_total
        rated_user.average_rating = new_avg

    db.commit()
    db.refresh(new_rating)
    return new_rating

@app.get("/users/{user_id}/ratings")
def get_user_ratings(user_id: str, db: Session = Depends(database.get_db)):
    ratings = db.query(models.Rating).filter(models.Rating.rated_id == user_id).all()
    if not ratings:
        return {"average_score": 0, "count": 0, "ratings": []}
    
    avg_score = sum(r.score for r in ratings) / len(ratings)
    
    return {
        "average_score": round(avg_score, 1),
        "count": len(ratings),
        "ratings": [
            {
                "score": r.score,
                "comment": r.comment,
                "created_at": r.created_at,
                "rater_name": db.query(models.User.full_name).filter(models.User.id == r.rater_id).scalar()
            } for r in ratings
        ]
    }


# --- Health Records API ---

@app.post("/dogs/{dog_id}/health-records", response_model=schemas.HealthRecordResponse)
def create_health_record(
    dog_id: str,
    record: schemas.HealthRecordCreate,
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(get_current_user)
):
    dog = db.query(models.Dog).filter(models.Dog.id == dog_id).first()
    if not dog:
        raise HTTPException(status_code=404, detail="Dog not found")
        
    if dog.owner_id != current_user.id: 
        # Check permissions? Maybe provider/admin can add records too?
        # For now, owner + admin + provider
        if current_user.role not in [models.UserRole.ADMIN, models.UserRole.PROVIDER]:
             raise HTTPException(status_code=403, detail="Not authorized")

    new_record = models.HealthRecord(
        id=str(uuid.uuid4()),
        dog_id=dog_id,
        record_type=record.record_type,
        date=datetime.fromisoformat(record.date.replace('Z', '+00:00')),
        next_due_date=datetime.fromisoformat(record.next_due_date.replace('Z', '+00:00')) if record.next_due_date else None,
        notes=record.notes
    )
    db.add(new_record)
    db.commit()
    db.refresh(new_record)
    return new_record

@app.get("/dogs/{dog_id}/health-records", response_model=List[schemas.HealthRecordResponse])
def get_dog_health_records(
    dog_id: str,
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(get_current_user)
):
    dog = db.query(models.Dog).filter(models.Dog.id == dog_id).first()
    if not dog:
        raise HTTPException(status_code=404, detail="Dog not found")
        
    # Allow owner, admin, provider to see records (provider needs to see vaccination status)
    if dog.owner_id != current_user.id and current_user.role not in [models.UserRole.ADMIN, models.UserRole.PROVIDER]:
        pass # Maybe restrict? For now open to authenticated users who know the dog ID? 
        # Actually better to restrict to owner/admin/provider who has context. 
        # Let's start with strict:
        # raise HTTPException(status_code=403, detail="Not authorized")
    
    return db.query(models.HealthRecord).filter(models.HealthRecord.dog_id == dog_id).all()

# --- Events API ---

@app.post("/events", response_model=schemas.EventResponse)
def create_event(
    event: schemas.EventCreate, 
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(get_current_user)
):
    # Optional: Check if user is admin or allowed to create events
    if current_user.role not in [models.UserRole.ADMIN, models.UserRole.PROVIDER]:
        raise HTTPException(status_code=403, detail="Not authorized to create events")
        
    new_event = models.Event(
        id=str(uuid.uuid4()),
        organizer_id=current_user.id,
        title=event.title,
        description=event.description,
        location=event.location,
        start_time=datetime.fromisoformat(event.start_time.replace('Z', '+00:00')),
        end_time=datetime.fromisoformat(event.end_time.replace('Z', '+00:00')),
        capacity=event.capacity,
        category=event.category,
        is_public=event.is_public
    )
    db.add(new_event)
    db.commit()
    db.refresh(new_event)
    return new_event

@app.get("/events", response_model=List[schemas.EventResponse])
def list_events(skip: int = 0, limit: int = 100, db: Session = Depends(database.get_db)):
    events = db.query(models.Event).offset(skip).limit(limit).all()
    for event in events:
        event.registrant_count = db.query(models.Registration).filter(
            models.Registration.event_id == event.id,
            models.Registration.status == "registered"
        ).count()
    return events 

@app.get("/events/{event_id}", response_model=schemas.EventResponse)
def get_event(event_id: str, db: Session = Depends(database.get_db)):
    event = db.query(models.Event).filter(models.Event.id == event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    
    event.registrant_count = db.query(models.Registration).filter(
        models.Registration.event_id == event.id,
        models.Registration.status == "registered"
    ).count()
    return event

@app.post("/events/{event_id}/register", response_model=schemas.RegistrationResponse)
def register_for_event(
    event_id: str,
    registration: schemas.RegistrationCreate,
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(get_current_user)
):
    event = db.query(models.Event).filter(models.Event.id == event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
        
    # Check capacity
    status = "registered"
    if event.capacity > 0:
        count = db.query(models.Registration).filter(models.Registration.event_id == event_id, models.Registration.status == "registered").count()
        if count >= event.capacity:
            if registration.join_waitlist:
                status = "waitlisted"
            else:
                raise HTTPException(status_code=400, detail="Event is full")
            
    # Check existing registration
    existing = db.query(models.Registration).filter(
        models.Registration.event_id == event_id,
        models.Registration.user_id == current_user.id
    ).first()
    if existing:
        raise HTTPException(status_code=400, detail="Already registered for this event")
        
    new_reg = models.Registration(
        id=str(uuid.uuid4()),
        event_id=event_id,
        user_id=current_user.id,
        dog_id=registration.dog_id,
        status=status,
        role=registration.role or "attendee",
        share_phone=registration.share_phone,
        ticket_token=secrets.token_urlsafe(16)
    )
    db.add(new_reg)
    db.flush()
    
    if registration.form_responses:
        for resp in registration.form_responses:
            new_resp = models.RegistrationResponse(
                id=str(uuid.uuid4()),
                registration_id=new_reg.id,
                field_id=resp.field_id,
                answer_value=resp.answer_value
            )
            db.add(new_resp)

    db.commit()
    db.refresh(new_reg)
    return new_reg


@app.post("/events/{event_id}/save")
def toggle_save_event(event_id: str, db: Session = Depends(database.get_db), current_user: models.User = Depends(get_current_user)):
    event = db.query(models.Event).filter(models.Event.id == event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
        
    existing = db.query(models.SavedEvent).filter(
        models.SavedEvent.event_id == event_id,
        models.SavedEvent.user_id == current_user.id
    ).first()
    
    if existing:
        db.delete(existing)
        db.commit()
        return {"status": "unsaved"}
    else:
        new_save = models.SavedEvent(
            id=str(uuid.uuid4()),
            event_id=event_id,
            user_id=current_user.id
        )
        db.add(new_save)
        db.commit()
        return {"status": "saved"}

@app.get("/saved-events", response_model=List[schemas.SavedEventResponse])
def get_saved_events(db: Session = Depends(database.get_db), current_user: models.User = Depends(get_current_user)):
    return db.query(models.SavedEvent).filter(models.SavedEvent.user_id == current_user.id).all()

@app.get("/events/{event_id}/form-fields", response_model=List[schemas.EventFormFieldResponse])
def get_event_form_fields(event_id: str, db: Session = Depends(database.get_db)):
    return db.query(models.EventFormField).filter(models.EventFormField.event_id == event_id).order_by(models.EventFormField.sort_order).all()

@app.post("/events/{event_id}/form-fields", response_model=List[schemas.EventFormFieldResponse])
def save_event_form_fields(
    event_id: str,
    fields: List[schemas.EventFormFieldCreate],
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(get_current_user)
):
    event = db.query(models.Event).filter(models.Event.id == event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
        
    if event.organizer_id != current_user.id and current_user.role != models.UserRole.ADMIN:
        raise HTTPException(status_code=403, detail="Not authorized to edit this event's form")
        
    db.query(models.EventFormField).filter(models.EventFormField.event_id == event_id).delete()
    
    new_fields = []
    for field in fields:
        new_field = models.EventFormField(
            id=str(uuid.uuid4()),
            event_id=event_id,
            field_type=field.field_type,
            label=field.label,
            options=field.options,
            is_required=field.is_required,
            sort_order=field.sort_order
        )
        db.add(new_field)
        new_fields.append(new_field)
        
    db.commit()
    for field in new_fields:
        db.refresh(field)
    return new_fields

@app.get("/events/{event_id}/responses", response_model=List[schemas.RegistrationWithResponses])
def get_event_responses(
    event_id: str,
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(get_current_user)
):
    event = db.query(models.Event).filter(models.Event.id == event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
        
    if event.organizer_id != current_user.id and current_user.role != models.UserRole.ADMIN:
        raise HTTPException(status_code=403, detail="Not authorized to view responses")
        
    registrations = db.query(models.Registration).filter(models.Registration.event_id == event_id).all()
    
    result = []
    for reg in registrations:
        reg_dict = {
            "id": reg.id,
            "event_id": reg.event_id,
            "user_id": reg.user_id,
            "status": reg.status,
            "role": reg.role,
            "share_phone": reg.share_phone,
            "created_at": reg.created_at,
            "responses": reg.responses
        }
        
        user = reg.user
        if user:
            reg_dict["user_name"] = user.full_name
            reg_dict["user_email"] = user.email
            if reg.share_phone or current_user.role == models.UserRole.ADMIN:
                reg_dict["user_phone"] = user.phone_number
                
        dog = reg.dog
        if dog:
            reg_dict["dog_name"] = dog.name
            
        result.append(reg_dict)
        
    return result

# --- Health & Wellness Hub API ---

@app.get("/health/wellness-score")
def get_wellness_score(
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(get_current_user)
):
    """Calculate aggregated wellness scores across all check-ins and health records"""
    # Fetch most recent check-in
    latest_checkin = db.query(models.CheckInData).filter(
        models.CheckInData.user_id == current_user.id
    ).order_by(models.CheckInData.created_at.desc()).first()
    
    if not latest_checkin:
        return {
            "overall_score": 0,
            "who5_score": 0,
            "pss_score": 0,
            "relationship_score": 0,
            "welfare_score": 0,
            "has_data": False
        }
        
    who5 = wellness_utils.calculate_who5_score(latest_checkin.who5_answers or {})
    pss = wellness_utils.calculate_pss10_score(latest_checkin.pss10_answers or {})
    rel = wellness_utils.calculate_relationship_score(latest_checkin.relationship_answers or {})
    wel = wellness_utils.calculate_dog_welfare_score(latest_checkin.welfare_snapshot or {})
    
    # Overall score (weighted average)
    # Give higher weight to WHO5 and Welfare
    overall = int((who5 * 0.3) + (wel * 0.3) + (rel * 0.2) + ((100 - pss) * 0.2))
    
    return {
        "overall_score": overall,
        "who5_score": who5,
        "pss_score": pss,
        "relationship_score": rel,
        "welfare_score": wel,
        "has_data": True,
        "last_checkin": latest_checkin.created_at
    }

@app.get("/health/summary")
def get_health_summary(
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(get_current_user)
):
    """Aggregate health data across all user dogs for dashboard display"""
    # 1. Get recent wellness checkin info
    latest_checkin = db.query(models.CheckInData).filter(
        models.CheckInData.user_id == current_user.id
    ).order_by(models.CheckInData.created_at.desc()).first()
    
    score = 0
    if latest_checkin:
        who5 = wellness_utils.calculate_who5_score(latest_checkin.who5_answers or {})
        wel = wellness_utils.calculate_dog_welfare_score(latest_checkin.welfare_snapshot or {})
        rel = wellness_utils.calculate_relationship_score(latest_checkin.relationship_answers or {})
        pss = wellness_utils.calculate_pss10_score(latest_checkin.pss10_answers or {})
        score = int((who5 * 0.3) + (wel * 0.3) + (rel * 0.2) + ((100 - pss) * 0.2))

    # 2. Get upcoming health records across all dogs
    user_dogs = db.query(models.Dog).filter(models.Dog.owner_id == current_user.id).all()
    dog_ids = [d.id for d in user_dogs]
    
    now = datetime.utcnow()
    upcoming_record = None
    if dog_ids:
        upcoming_record = db.query(models.HealthRecord).filter(
            models.HealthRecord.dog_id.in_(dog_ids),
            models.HealthRecord.next_due_date > now
        ).order_by(models.HealthRecord.next_due_date.asc()).first()
        
    alert_text = None
    if upcoming_record:
        dog = next((d for d in user_dogs if d.id == upcoming_record.dog_id), None)
        dog_name = dog.name if dog else "Your dog"
        days_until = (upcoming_record.next_due_date.replace(tzinfo=None) - now).days
        if days_until == 0:
            alert_text = f"{dog_name}'s {upcoming_record.record_type} is due TODAY!"
        elif days_until < 14:
            alert_text = f"{dog_name}'s {upcoming_record.record_type} is due in {days_until} days."
        else:
            alert_text = f"Upcoming: {dog_name}'s {upcoming_record.record_type} on {upcoming_record.next_due_date.strftime('%b %d')}."
            
    return {
        "overall_score": score,
        "upcoming_alert": alert_text,
        "has_data": len(user_dogs) > 0
    }


@app.get("/health/advisor/{dog_id}")
def get_health_advisor_insights(
    dog_id: str,
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(get_current_user)
):
    """Provide AI-driven health insights with heuristic fallback"""
    dog = db.query(models.Dog).filter(models.Dog.id == dog_id).first()
    if not dog:
        raise HTTPException(status_code=404, detail="Dog not found")
        
    # Gather Context for AI
    # 1. Recent Health Records
    records = db.query(models.HealthRecord).filter(
        models.HealthRecord.dog_id == dog_id
    ).order_by(models.HealthRecord.date.desc()).limit(5).all()
    
    # 2. Latest Wellness Stats
    latest_checkin = db.query(models.CheckInData).filter(
        models.CheckInData.user_id == current_user.id,
        models.CheckInData.dog_id == dog_id
    ).order_by(models.CheckInData.created_at.desc()).first()
    
    wellness_stats = {}
    if latest_checkin:
        wellness_stats = {
            "who5_score": wellness_utils.calculate_who5_score(latest_checkin.who5_answers or {}),
            "pss_score": wellness_utils.calculate_pss10_score(latest_checkin.pss10_answers or {}),
            "relationship_score": wellness_utils.calculate_relationship_score(latest_checkin.relationship_answers or {}),
            "welfare_score": wellness_utils.calculate_dog_welfare_score(latest_checkin.welfare_snapshot or {}),
        }
        wellness_stats["overall_score"] = int((wellness_stats["who5_score"] * 0.3) + (wellness_stats["welfare_score"] * 0.3) + (wellness_stats["relationship_score"] * 0.2) + ((100 - wellness_stats["pss_score"]) * 0.2))

    # Try Gemini AI First
    ai_response = gemini_advisor.generate_health_insights(
        dog_data={"name": dog.name, "breed": dog.breed, "age": dog.age, "weight": dog.weight},
        records=[{"record_type": r.record_type, "notes": r.notes, "date": str(r.date)} for r in records],
        wellness_stats=wellness_stats
    )
    
    if ai_response:
        return {
            "dog_name": dog.name,
            "breed": dog.breed,
            "insights": ai_response.get("insights", []),
            "pro_tip": ai_response.get("pro_tip", "Stay consistent with health checks!"),
            "engine": "Gemini AI"
        }

    # Fallback to Heuristics if AI fails
    insights = []
    breed_key = (dog.breed or "").lower()
    if "gsd" in breed_key or "german shepherd" in breed_key:
        insights.append("German Shepherds are prone to hip dysplasia. Ensure regular joint-focused exercise.")
    elif "rot" in breed_key or "rottweiler" in breed_key:
        insights.append("Rottweilers have a higher risk of heart issues. Maintain a lean weight.")
        
    latest_vax = next((r for r in records if r.record_type == "vaccination"), None)
    if not latest_vax:
        insights.append("We haven't recorded any vaccinations yet. Please update medical history.")
        
    if not insights:
        insights.append("Continue tracking daily activity and nutrition.")
        
    return {
        "dog_name": dog.name,
        "breed": dog.breed,
        "insights": insights,
        "pro_tip": "Regular check-ins improve accuracy by 40%.",
        "engine": "Heuristic Engine"
    }


@app.get("/my-registrations", response_model=List[schemas.RegistrationResponse])
def get_my_registrations(db: Session = Depends(database.get_db), current_user: models.User = Depends(get_current_user)):
    return db.query(models.Registration).filter(models.Registration.user_id == current_user.id).all()


@app.put("/services/{service_id}", response_model=schemas.ServiceResponse)
def update_service(
    service_id: str,
    service_update: schemas.ServiceBase,
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(get_current_user)
):
    service = db.query(models.Service).filter(models.Service.id == service_id).first()
    if not service:
        raise HTTPException(status_code=404, detail="Service not found")
    
    if service.provider_id != current_user.id and current_user.role != models.UserRole.ADMIN:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    update_data = service_update.dict()
    # Apply 20% markup if price is updated
    if "price" in update_data:
        update_data["price"] = round(update_data["price"] * 1.20, 2)

    for key, value in update_data.items():
        setattr(service, key, value)
    
    db.commit()
    db.refresh(service)
    return service

@app.delete("/services/{service_id}")
def delete_service(
    service_id: str,
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(get_current_user)
):
    service = db.query(models.Service).filter(models.Service.id == service_id).first()
    if not service:
        raise HTTPException(status_code=404, detail="Service not found")
    
    if service.provider_id != current_user.id and current_user.role != models.UserRole.ADMIN:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    db.delete(service)
    db.commit()
    return {"message": "Service deleted successfully"}

# ... (Orders API) ...

import openpyxl
from openpyxl.styles import Font

@app.post("/registrations/{registration_id}/checkin", response_model=schemas.RegistrationResponse)
def check_in(
    registration_id: str,
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(get_current_user)
):
    # Verify registration exists and belongs to user (or admin)
    reg = db.query(models.Registration).filter(models.Registration.id == registration_id).first()
    if not reg:
        raise HTTPException(status_code=404, detail="Registration not found")
        
    # Allow self-checkin (if event allows) or Admin/Provider checkin
    if reg.user_id != current_user.id and current_user.role not in [models.UserRole.ADMIN, models.UserRole.PROVIDER]:
        raise HTTPException(status_code=403, detail="Not authorized")
        
    reg.status = "checked-in"
    reg.check_in_time = datetime.utcnow()
    db.commit()
    db.refresh(reg)
    return reg

@app.get("/admin/export")
def export_data(
    type: str, 
    event_id: Optional[str] = None, 
    date_from: Optional[str] = None,
    location: Optional[str] = None,
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(get_current_user)
):
    # Expanded Access: Allow Admins AND Partners/Providers (if authorized)
    if current_user.role not in [models.UserRole.ADMIN, models.UserRole.PROVIDER]:
        raise HTTPException(status_code=403, detail="Not authorized")
        
    # Create Workbook
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = f"{type.capitalize()} Data"
    
    # Headers Style
    header_font = Font(bold=True)
    
    if type == "registrations":
        query = db.query(models.Registration)
        if event_id:
            query = query.filter(models.Registration.event_id == event_id)
        
        registrations = query.all()
        headers = ["Registration ID", "Event ID", "User ID", "Dog ID", "Status", "Role", "Check-in Time", "Created At"]
        ws.append(headers)
        for cell in ws[1]: cell.font = header_font
        
        for reg in registrations:
            ws.append([reg.id, reg.event_id, reg.user_id, reg.dog_id, reg.status, reg.role, str(reg.check_in_time), str(reg.created_at)])
            
    elif type == "events":
        events = db.query(models.Event).all()
        headers = ["Event ID", "Title", "Date", "Location", "Organizer ID", "Category"]
        ws.append(headers)
        for cell in ws[1]: cell.font = header_font
        for ev in events:
            ws.append([ev.id, ev.title, str(ev.start_time), ev.location, ev.organizer_id, ev.category])

    elif type == "users":
        users = db.query(models.User).all()
        headers = ["User ID", "Full Name", "Email", "Role", "Phone", "Country", "Created At"]
        ws.append(headers)
        for cell in ws[1]: cell.font = header_font
        for u in users:
            ws.append([u.id, u.full_name, u.email, u.role, u.phone_number, u.country, str(u.created_at)])

    elif type == "orders":
        orders = db.query(models.Order).all()
        headers = ["Order ID", "Buyer ID", "Service ID", "Amount", "Commission", "Payout", "Status", "Created At"]
        ws.append(headers)
        for cell in ws[1]: cell.font = header_font
        for o in orders:
            ws.append([o.id, o.buyer_id, o.service_id, o.amount, o.commission, o.payout, o.status, str(o.created_at)])

    elif type == "dogs":
        dogs = db.query(models.Dog).all()
        headers = ["Dog ID", "Name", "Breed", "Owner ID", "Age", "Nose-PID", "Created At"]
        ws.append(headers)
        for cell in ws[1]: cell.font = header_font
        for d in dogs:
            ws.append([d.id, d.name, d.breed, d.owner_id, d.age, "Yes" if d.nose_print_image else "No", str(d.created_at) if hasattr(d, 'created_at') else "N/A"])

    elif type == "cases":
        cases = db.query(models.CaseReport).all()
        headers = ["Case ID", "Title", "Type", "Status", "Author ID", "Approved", "Created At"]
        ws.append(headers)
        for cell in ws[1]: cell.font = header_font
        for c in cases:
            ws.append([c.id, c.title, c.case_type, c.status, c.author_id, "Yes" if c.is_approved else "No", str(c.created_at)])

    elif type == "community":
        posts = db.query(models.CommunityMessage).all()
        headers = ["Post ID", "Author ID", "Content", "Flags", "Hidden", "Created At"]
        ws.append(headers)
        for cell in ws[1]: cell.font = header_font
        for p in posts:
            ws.append([p.id, p.author_id, p.content, p.flag_count, "Yes" if p.is_hidden else "No", str(p.created_at)])

    elif type == "support":
        tickets = db.query(models.SupportTicket).all()
        headers = ["Ticket ID", "User ID", "Subject", "Status", "Created At", "Updated At"]
        ws.append(headers)
        for cell in ws[1]: cell.font = header_font
        for t in tickets:
            ws.append([t.id, t.user_id, t.subject, t.status, str(t.created_at), str(t.updated_at)])

            
    # Save to buffer
    stream = io.BytesIO()
    wb.save(stream)
    stream.seek(0)
            
    response = StreamingResponse(iter([stream.getvalue()]), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    response.headers["Content-Disposition"] = f"attachment; filename=export_{type}_{datetime.utcnow().timestamp()}.xlsx"
    return response


# --- Lovedogs 360 Event Specific API ---

@app.get("/events/{event_id}/journey", response_model=schemas.ProgramJourneyResponse)
def get_program_journey(
    event_id: str,
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(get_current_user)
):
    journey = db.query(models.ProgramJourney).filter(
        models.ProgramJourney.event_id == event_id,
        models.ProgramJourney.user_id == current_user.id
    ).first()
    
    if not journey:
        # Create a baseline journey if none exists (Auto-start T1)
        # In a real app we might only do this after they register, but for UX ease:
        journey = models.ProgramJourney(
            id=str(uuid.uuid4()),
            event_id=event_id,
            user_id=current_user.id,
            progress_percentage=0.0,
            current_timepoint="T1"
        )
        db.add(journey)
        db.commit()
        db.refresh(journey)

    return journey

@app.post("/events/{event_id}/sync")
def bulk_sync_event_data(
    event_id: str,
    payload: schemas.BulkSyncPayload,
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(get_current_user)
):
    added_checkins = 0
    added_observations = 0

    # Sync Check-ins 
    for c in payload.checkins:
        # Prevent duplicates based on timepoint per user per event
        existing = db.query(models.CheckInData).filter(
            models.CheckInData.event_id == event_id,
            models.CheckInData.user_id == c.user_id,
            models.CheckInData.timepoint == c.timepoint
        ).first()

        if not existing:
            new_checkin = models.CheckInData(
                id=str(uuid.uuid4()),
                event_id=event_id,
                user_id=c.user_id,
                dog_id=c.dog_id,
                timepoint=c.timepoint,
                who5_answers=c.who5_answers,
                pss10_answers=c.pss10_answers,
                relationship_answers=c.relationship_answers,
                welfare_snapshot=c.welfare_snapshot
            )
            db.add(new_checkin)
            added_checkins += 1

            # Update Journey Progress automatically
            journey = db.query(models.ProgramJourney).filter(
                models.ProgramJourney.event_id == event_id,
                models.ProgramJourney.user_id == c.user_id
            ).first()
            if journey:
                # Basic Logic for progress bumping
                if c.timepoint == "T1": 
                    journey.current_timepoint = "T2"
                    journey.progress_percentage = max(journey.progress_percentage, 25.0)
                elif c.timepoint == "T2":
                    journey.current_timepoint = "T3"
                    journey.progress_percentage = max(journey.progress_percentage, 50.0)
                elif c.timepoint == "T3":
                    journey.current_timepoint = "T4"
                    journey.progress_percentage = max(journey.progress_percentage, 75.0)
                elif c.timepoint == "T4":
                    journey.progress_percentage = 100.0

    # Sync Observations (Facilitators / Vets)
    for obs in payload.observations:
        new_obs = models.LiveObservation(
            id=str(uuid.uuid4()),
            event_id=event_id,
            observer_id=current_user.id,
            participant_id=obs.participant_id,
            dog_id=obs.dog_id,
            behavior=obs.behavior,
            intensity=obs.intensity,
            notes=obs.notes,
            timestamp=obs.timestamp.replace(tzinfo=None) if obs.timestamp else datetime.utcnow(),
            is_offline_sync=obs.is_offline_sync,
            synced_at=datetime.utcnow()
        )
        db.add(new_obs)
        added_observations += 1

    db.commit()
    return {"message": "Sync successful", "checkins_synced": added_checkins, "observations_synced": added_observations}

@app.post("/events/{event_id}/live-log", response_model=schemas.LiveObservationResponse)
def live_log_observation(
    event_id: str,
    observation: schemas.LiveObservationCreate,
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(get_current_user)
):
    if current_user.role not in [models.UserRole.ADMIN, models.UserRole.PROVIDER]:
        raise HTTPException(status_code=403, detail="Not authorized to log observations")

    new_obs = models.LiveObservation(
        id=str(uuid.uuid4()),
        event_id=event_id,
        observer_id=current_user.id,
        participant_id=observation.participant_id,
        dog_id=observation.dog_id,
        behavior=observation.behavior,
        intensity=observation.intensity,
        notes=observation.notes,
        timestamp=observation.timestamp.replace(tzinfo=None) if observation.timestamp else datetime.utcnow(),
        is_offline_sync=False,
        synced_at=datetime.utcnow()
    )
    db.add(new_obs)
    db.commit()
    db.refresh(new_obs)
    return new_obs


# --- Admin Dashboard API ---

def require_admin(current_user: models.User = Depends(get_current_user)):
    if current_user.role != models.UserRole.ADMIN:
        raise HTTPException(status_code=403, detail="Admin access required")
    return current_user

@app.get("/admin/stats")
def admin_stats(db: Session = Depends(database.get_db), admin: models.User = Depends(require_admin)):
    total_users = db.query(models.User).count()
    total_services = db.query(models.Service).count()
    total_orders = db.query(models.Order).count()
    total_events = db.query(models.Event).count()
    
    from sqlalchemy import func
    total_revenue = db.query(func.coalesce(func.sum(models.Order.amount), 0)).filter(
        models.Order.status.in_([models.OrderStatus.PAID, models.OrderStatus.COMPLETED, models.OrderStatus.SETTLED])
    ).scalar()
    total_commission = db.query(func.coalesce(func.sum(models.Order.commission), 0)).filter(
        models.Order.status.in_([models.OrderStatus.PAID, models.OrderStatus.COMPLETED, models.OrderStatus.SETTLED])
    ).scalar()
    
    return {
        "total_users": total_users,
        "total_services": total_services,
        "total_orders": total_orders,
        "total_events": total_events,
        "total_revenue": round(float(total_revenue), 2),
        "total_commission": round(float(total_commission), 2)
    }

@app.get("/admin/users")
def admin_list_users(db: Session = Depends(database.get_db), admin: models.User = Depends(require_admin)):
    users = db.query(models.User).all()
    return [
        {
            "id": u.id,
            "full_name": u.full_name,
            "email": u.email,
            "phone_number": u.phone_number,
            "role": u.role,
            "bio": u.bio
        }
        for u in users
    ]

@app.get("/admin/orders")
def admin_list_orders(db: Session = Depends(database.get_db), admin: models.User = Depends(require_admin)):
    orders = db.query(models.Order).all()
    result = []
    for order in orders:
        service = db.query(models.Service).filter(models.Service.id == order.service_id).first()
        buyer = db.query(models.User).filter(models.User.id == order.buyer_id).first()
        provider = None
        if service:
            provider = db.query(models.User).filter(models.User.id == service.provider_id).first()
        result.append({
            "id": order.id,
            "buyer_name": buyer.full_name if buyer else "Unknown",
            "buyer_email": buyer.email if buyer else "",
            "provider_name": provider.full_name if provider else "Unknown",
            "provider_id": service.provider_id if service else None,
            "service_title": service.title if service else "Unknown",
            "amount": order.amount,
            "commission": order.commission,
            "payout": order.payout,
            "status": order.status,
            "share_phone": order.share_phone,
            "form_responses": [
                {
                    "label": r.field.label,
                    "answer": r.answer_value
                } for r in order.form_responses
            ],
            "created_at": str(order.created_at) if order.created_at else None
        })
    return result

@app.post("/admin/orders/{order_id}/complete")
def admin_complete_order(
    order_id: str,
    db: Session = Depends(database.get_db),
    admin: models.User = Depends(require_admin)
):
    """Admin confirms service delivery — transitions order from PAID to COMPLETED."""
    order = db.query(models.Order).filter(models.Order.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    if order.status != models.OrderStatus.PAID:
        raise HTTPException(
            status_code=400,
            detail=f"Order must be in 'paid' status to mark as completed. Current status: {order.status}"
        )

    order.status = models.OrderStatus.COMPLETED
    db.commit()
    return {"message": "Order marked as completed. Seller payout is now ready for approval.", "status": order.status}

@app.post("/admin/orders/{order_id}/settle")
def admin_settle_order(
    order_id: str,
    db: Session = Depends(database.get_db),
    admin: models.User = Depends(require_admin)
):
    """Admin approves seller payout — transitions order from COMPLETED to SETTLED and credits the seller's wallet."""
    order = db.query(models.Order).filter(models.Order.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    if order.status != models.OrderStatus.COMPLETED:
        raise HTTPException(
            status_code=400,
            detail=f"Order must be in 'completed' status to settle. Current status: {order.status}"
        )

    # Find the service provider
    service = db.query(models.Service).filter(models.Service.id == order.service_id).first()
    if not service:
        raise HTTPException(status_code=404, detail="Service not found for this order")

    provider_id = service.provider_id
    payout_amount = order.payout or 0

    # Credit the provider's wallet (create wallet if it doesn't exist)
    wallet = db.query(models.Transaction).filter(
        models.Transaction.order_id == order_id,
        models.Transaction.type == "payout"
    ).first()

    if wallet:
        raise HTTPException(status_code=400, detail="Payout transaction already exists for this order")

    # Create payout transaction record
    tx = models.Transaction(
        id=str(uuid.uuid4()),
        order_id=order_id,
        user_id=provider_id,
        amount=payout_amount,
        type="payout",
        status="completed"
    )
    db.add(tx)

    # Update order status to SETTLED
    order.status = models.OrderStatus.SETTLED
    db.commit()

    # Send notification to provider
    try:
        provider = db.query(models.User).filter(models.User.id == provider_id).first()
        notification = models.Notification(
            id=str(uuid.uuid4()),
            user_id=provider_id,
            title="Payout Approved! 💰",
            message=f"Your payout of KES {payout_amount:,.2f} for '{service.title}' has been approved and settled.",
            type="info"
        )
        db.add(notification)
        db.commit()
    except Exception:
        pass  # Non-critical — don't fail the settlement

    return {
        "message": f"Payout of KES {payout_amount:,.2f} approved and settled for provider.",
        "status": order.status,
        "payout_amount": payout_amount
    }

@app.get("/admin/services")
def admin_list_services(db: Session = Depends(database.get_db), admin: models.User = Depends(require_admin)):
    services = db.query(models.Service).all()
    result = []
    for s in services:
        provider = db.query(models.User).filter(models.User.id == s.provider_id).first()
        result.append({
            "id": s.id,
            "title": s.title,
            "description": s.description,
            "price": s.price,
            "category": s.category,
            "item_type": s.item_type,
            "is_published": s.is_published,
            "provider_name": provider.full_name if provider else "Unknown",
            "provider_id": s.provider_id,
            "image_url": s.image_url
        })
    return result


class ApprovalRequest(BaseModel):
    is_approved: bool
    rejection_reason: Optional[str] = None

@app.get("/admin/pending-approvals")
def admin_list_pending(db: Session = Depends(database.get_db), admin: models.User = Depends(require_admin)):
    pending_services = db.query(models.Service).filter(models.Service.admin_approved == False).all()
    pending_reports = db.query(models.CaseReport).filter(models.CaseReport.is_approved == False).all()
    
    services_out = []
    for s in pending_services:
        provider = db.query(models.User).filter(models.User.id == s.provider_id).first()
        services_out.append({
            "id": s.id, "title": s.title, "description": s.description, "price": s.price,
            "category": s.category, "item_type": s.item_type,
            "provider_name": provider.full_name if provider else "Unknown",
            "provider_id": s.provider_id
        })
    
    reports_out = []
    for r in pending_reports:
        author = db.query(models.User).filter(models.User.id == r.author_id).first()
        reports_out.append({
            "id": r.id, "title": r.title, "description": r.description,
            "case_type": r.case_type, "location": r.location,
            "author_name": author.full_name if author else "Unknown",
            "created_at": str(r.created_at)
        })
        
    return {
        "pending_services": services_out,
        "pending_reports": reports_out
    }

@app.post("/admin/approve/{item_type}/{item_id}")
def admin_approve_item(
    item_type: str, 
    item_id: str, 
    req: ApprovalRequest,
    db: Session = Depends(database.get_db), 
    admin: models.User = Depends(require_admin)
):
    if item_type == "service":
        item = db.query(models.Service).filter(models.Service.id == item_id).first()
        if not item: raise HTTPException(status_code=404, detail="Service not found")
        item.admin_approved = req.is_approved
        item.rejection_reason = req.rejection_reason
    elif item_type == "report":
        item = db.query(models.CaseReport).filter(models.CaseReport.id == item_id).first()
        if not item: raise HTTPException(status_code=404, detail="Report not found")
        item.is_approved = req.is_approved
        item.rejection_reason = req.rejection_reason
    else:
        raise HTTPException(status_code=400, detail="Invalid item type")
        
    db.commit()
    
    # Notify User
    if req.is_approved:
        title = f"{item_type.capitalize()} Approved"
        msg = f"Your {item_type} '{item.title}' has been approved and is now visible to the public."
        notif_type = "approval"
    else:
        title = f"{item_type.capitalize()} Rejected"
        msg = f"Your {item_type} '{item.title}' was rejected."
        if req.rejection_reason:
            msg += f" Reason: {req.rejection_reason}"
        notif_type = "rejection"
    
    recipient_id = item.provider_id if item_type == "service" else item.author_id
    create_notification(db, recipient_id, title, msg, notif_type)

    return {"message": "Success", "is_approved": req.is_approved}

@app.get("/spotlight", response_model=List[schemas.SpotlightResponse])
def get_spotlight(db: Session = Depends(database.get_db)):
    # Return all active spotlights as an array
    spotlights = db.query(models.Spotlight).filter(models.Spotlight.is_active == True).order_by(models.Spotlight.updated_at.desc()).all()
    return spotlights

@app.get("/admin/spotlight", response_model=List[schemas.SpotlightResponse])
def get_admin_spotlight(db: Session = Depends(database.get_db), admin: models.User = Depends(require_admin)):
    spotlights = db.query(models.Spotlight).order_by(models.Spotlight.updated_at.desc()).all()
    return spotlights

@app.post("/admin/spotlight", response_model=schemas.SpotlightResponse)
def create_admin_spotlight(
    spotlight_in: schemas.SpotlightBase,
    db: Session = Depends(database.get_db),
    admin: models.User = Depends(require_admin)
):
    spotlight = models.Spotlight(
        title=spotlight_in.title,
        description=spotlight_in.description,
        image_url=spotlight_in.image_url,
        target_route=spotlight_in.target_route,
        target_id=spotlight_in.target_id,
        is_active=spotlight_in.is_active
    )
    db.add(spotlight)
    db.commit()
    db.refresh(spotlight)
    return spotlight

@app.delete("/admin/spotlight/{spotlight_id}")
def delete_admin_spotlight(
    spotlight_id: int,
    db: Session = Depends(database.get_db),
    admin: models.User = Depends(require_admin)
):
    spotlight = db.query(models.Spotlight).filter(models.Spotlight.id == spotlight_id).first()
    if not spotlight:
        raise HTTPException(status_code=404, detail="Spotlight not found")
    db.delete(spotlight)
    db.commit()
    return {"message": "Spotlight removed"}

# =====================================================
# Admin Analytics & Enhanced Management Endpoints
# =====================================================

@app.get("/admin/analytics")
def admin_analytics(db: Session = Depends(database.get_db), admin: models.User = Depends(require_admin)):
    from sqlalchemy import func
    now = datetime.utcnow()
    thirty_days_ago = now - timedelta(days=30)
    sixty_days_ago = now - timedelta(days=60)

    # --- Growth metrics (current 30d vs previous 30d) ---
    new_users_30d = db.query(models.User).filter(models.User.created_at >= thirty_days_ago).count()
    new_users_prev_30d = db.query(models.User).filter(
        models.User.created_at >= sixty_days_ago, models.User.created_at < thirty_days_ago
    ).count()
    new_orders_30d = db.query(models.Order).filter(models.Order.created_at >= thirty_days_ago).count()
    new_orders_prev_30d = db.query(models.Order).filter(
        models.Order.created_at >= sixty_days_ago, models.Order.created_at < thirty_days_ago
    ).count()

    paid_statuses = [models.OrderStatus.PAID, models.OrderStatus.COMPLETED, models.OrderStatus.SETTLED]
    revenue_30d = float(db.query(func.coalesce(func.sum(models.Order.amount), 0)).filter(
        models.Order.status.in_(paid_statuses), models.Order.created_at >= thirty_days_ago
    ).scalar())
    revenue_prev_30d = float(db.query(func.coalesce(func.sum(models.Order.amount), 0)).filter(
        models.Order.status.in_(paid_statuses),
        models.Order.created_at >= sixty_days_ago, models.Order.created_at < thirty_days_ago
    ).scalar())

    # --- Users by role ---
    role_counts = db.query(models.User.role, func.count(models.User.id)).group_by(models.User.role).all()
    users_by_role = {role: count for role, count in role_counts}

    # --- Orders by status ---
    status_counts = db.query(models.Order.status, func.count(models.Order.id)).group_by(models.Order.status).all()
    orders_by_status = {status: count for status, count in status_counts}

    # --- Top services by order count ---
    top_services_q = db.query(
        models.Service.title,
        func.count(models.Order.id).label("order_count"),
        func.coalesce(func.sum(models.Order.amount), 0).label("revenue")
    ).join(models.Order, models.Order.service_id == models.Service.id).group_by(
        models.Service.id, models.Service.title
    ).order_by(func.count(models.Order.id).desc()).limit(5).all()
    top_services = [{"title": t, "order_count": c, "revenue": round(float(r), 2)} for t, c, r in top_services_q]

    # --- Recent activity feed ---
    recent_activity = []
    recent_users = db.query(models.User).order_by(models.User.created_at.desc()).limit(5).all()
    for u in recent_users:
        recent_activity.append({
            "type": "registration", "icon": "person-add",
            "description": f"{u.full_name} joined as {u.role}",
            "time": str(u.created_at)
        })
    recent_orders = db.query(models.Order).order_by(models.Order.created_at.desc()).limit(5).all()
    for o in recent_orders:
        svc = db.query(models.Service.title).filter(models.Service.id == o.service_id).scalar()
        recent_activity.append({
            "type": "order", "icon": "cart",
            "description": f"New order for {svc or 'Unknown'}",
            "time": str(o.created_at)
        })
    recent_cases = db.query(models.CaseReport).order_by(models.CaseReport.created_at.desc()).limit(3).all()
    for c in recent_cases:
        recent_activity.append({
            "type": "case", "icon": "alert-circle",
            "description": f"Case reported: {c.title}",
            "time": str(c.created_at)
        })
    recent_activity.sort(key=lambda x: x["time"], reverse=True)
    recent_activity = recent_activity[:10]

    # --- Community stats ---
    total_cases = db.query(models.CaseReport).count()
    open_cases = db.query(models.CaseReport).filter(models.CaseReport.status == "open").count()
    total_dogs = db.query(models.Dog).count()
    total_community_posts = db.query(models.CommunityMessage).count()
    flagged_posts = db.query(models.CommunityMessage).filter(models.CommunityMessage.flag_count > 0).count()

    # --- Support stats ---
    open_tickets = db.query(models.SupportTicket).filter(models.SupportTicket.status == "Open").count()
    total_tickets = db.query(models.SupportTicket).count()

    # --- Pending approvals count ---
    pending_services = db.query(models.Service).filter(models.Service.admin_approved == False).count()
    pending_reports = db.query(models.CaseReport).filter(models.CaseReport.is_approved == False).count()

    # --- Monthly revenue (last 6 months) ---
    monthly_revenue = []
    month_names = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    for i in range(5, -1, -1):
        month_start = (now.replace(day=1) - timedelta(days=i * 30)).replace(day=1)
        if month_start.month == 12:
            month_end = month_start.replace(year=month_start.year + 1, month=1)
        else:
            month_end = month_start.replace(month=month_start.month + 1)
        rev = float(db.query(func.coalesce(func.sum(models.Order.amount), 0)).filter(
            models.Order.status.in_(paid_statuses),
            models.Order.created_at >= month_start, models.Order.created_at < month_end
        ).scalar())
        comm = float(db.query(func.coalesce(func.sum(models.Order.commission), 0)).filter(
            models.Order.status.in_(paid_statuses),
            models.Order.created_at >= month_start, models.Order.created_at < month_end
        ).scalar())
        monthly_revenue.append({
            "month": month_names[month_start.month - 1],
            "revenue": round(rev, 2),
            "commission": round(comm, 2)
        })

    # --- Event stats ---
    total_events = db.query(models.Event).count()
    upcoming_events = db.query(models.Event).filter(models.Event.start_time > now).count()
    total_registrations = db.query(models.Registration).count()

    return {
        "total_users": db.query(models.User).count(),
        "total_services": db.query(models.Service).count(),
        "total_orders": db.query(models.Order).count(),
        "total_events": total_events,
        "total_revenue": round(float(db.query(func.coalesce(func.sum(models.Order.amount), 0)).filter(
            models.Order.status.in_(paid_statuses)).scalar()), 2),
        "total_commission": round(float(db.query(func.coalesce(func.sum(models.Order.commission), 0)).filter(
            models.Order.status.in_(paid_statuses)).scalar()), 2),
        "new_users_30d": new_users_30d,
        "new_users_prev_30d": new_users_prev_30d,
        "new_orders_30d": new_orders_30d,
        "new_orders_prev_30d": new_orders_prev_30d,
        "revenue_30d": round(revenue_30d, 2),
        "revenue_prev_30d": round(revenue_prev_30d, 2),
        "users_by_role": users_by_role,
        "orders_by_status": orders_by_status,
        "top_services": top_services,
        "recent_activity": recent_activity,
        "total_cases": total_cases,
        "open_cases": open_cases,
        "total_dogs": total_dogs,
        "total_community_posts": total_community_posts,
        "flagged_posts": flagged_posts,
        "open_tickets": open_tickets,
        "total_tickets": total_tickets,
        "pending_services": pending_services,
        "pending_reports": pending_reports,
        "monthly_revenue": monthly_revenue,
        "upcoming_events": upcoming_events,
        "total_registrations": total_registrations
    }


@app.get("/admin/events")
def admin_list_events(db: Session = Depends(database.get_db), admin: models.User = Depends(require_admin)):
    events = db.query(models.Event).order_by(models.Event.start_time.desc()).all()
    result = []
    for e in events:
        organizer = db.query(models.User).filter(models.User.id == e.organizer_id).first()
        reg_count = db.query(models.Registration).filter(models.Registration.event_id == e.id).count()
        checkin_count = db.query(models.Registration).filter(
            models.Registration.event_id == e.id, models.Registration.status == "checked-in"
        ).count()
        result.append({
            "id": e.id, "title": e.title, "description": e.description,
            "location": e.location, "start_time": str(e.start_time), "end_time": str(e.end_time),
            "capacity": e.capacity, "category": e.category, "is_public": e.is_public,
            "organizer_name": organizer.full_name if organizer else "Unknown",
            "registration_count": reg_count, "checkin_count": checkin_count,
            "created_at": str(e.created_at)
        })
    return result


@app.delete("/admin/events/{event_id}")
def admin_delete_event(event_id: str, db: Session = Depends(database.get_db), admin: models.User = Depends(require_admin)):
    event = db.query(models.Event).filter(models.Event.id == event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    # Delete related registrations first
    db.query(models.Registration).filter(models.Registration.event_id == event_id).delete()
    db.query(models.EventFormField).filter(models.EventFormField.event_id == event_id).delete()
    db.delete(event)
    db.commit()
    return {"message": "Event deleted"}


@app.get("/admin/community")
def admin_list_community(db: Session = Depends(database.get_db), admin: models.User = Depends(require_admin)):
    posts = db.query(models.CommunityMessage).order_by(models.CommunityMessage.created_at.desc()).limit(50).all()
    result = []
    for p in posts:
        author = db.query(models.User).filter(models.User.id == p.author_id).first()
        reaction_count = db.query(models.ChatReaction).filter(models.ChatReaction.message_id == p.id).count()
        result.append({
            "id": p.id, "content": p.content, "is_poll": p.is_poll,
            "flag_count": p.flag_count, "is_hidden": p.is_hidden,
            "hashtags": p.hashtags or [], "reaction_count": reaction_count,
            "author_name": author.full_name if author else "Unknown",
            "author_id": p.author_id,
            "created_at": str(p.created_at)
        })
    return result


@app.post("/admin/community/{post_id}/hide")
def admin_hide_post(post_id: str, db: Session = Depends(database.get_db), admin: models.User = Depends(require_admin)):
    post = db.query(models.CommunityMessage).filter(models.CommunityMessage.id == post_id).first()
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")
    post.is_hidden = not post.is_hidden
    db.commit()
    return {"message": f"Post {'hidden' if post.is_hidden else 'restored'}", "is_hidden": post.is_hidden}


@app.delete("/admin/community/{post_id}")
def admin_delete_post(post_id: str, db: Session = Depends(database.get_db), admin: models.User = Depends(require_admin)):
    post = db.query(models.CommunityMessage).filter(models.CommunityMessage.id == post_id).first()
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")
    db.query(models.ChatReaction).filter(models.ChatReaction.message_id == post_id).delete()
    db.query(models.CommunityPollVote).filter(models.CommunityPollVote.message_id == post_id).delete()
    db.delete(post)
    db.commit()
    return {"message": "Post deleted"}


class AnnouncementCreate(BaseModel):
    title: str
    message: str
    target_audience: Optional[str] = "all"

@app.get("/admin/announcements")
def admin_list_announcements(db: Session = Depends(database.get_db), admin: models.User = Depends(require_admin)):
    announcements = db.query(models.Announcement).order_by(models.Announcement.created_at.desc()).all()
    return [{"id": a.id, "title": a.title, "message": a.message, "target_audience": a.target_audience,
             "created_at": str(a.created_at)} for a in announcements]

@app.post("/admin/announcements")
def admin_create_announcement(data: AnnouncementCreate, db: Session = Depends(database.get_db), admin: models.User = Depends(require_admin)):
    ann = models.Announcement(id=str(uuid.uuid4()), title=data.title, message=data.message, target_audience=data.target_audience)
    db.add(ann)
    db.commit()
    db.refresh(ann)
    return {"id": ann.id, "title": ann.title, "message": ann.message, "target_audience": ann.target_audience, "created_at": str(ann.created_at)}

@app.delete("/admin/announcements/{announcement_id}")
def admin_delete_announcement(announcement_id: str, db: Session = Depends(database.get_db), admin: models.User = Depends(require_admin)):
    ann = db.query(models.Announcement).filter(models.Announcement.id == announcement_id).first()
    if not ann:
        raise HTTPException(status_code=404, detail="Announcement not found")
    db.delete(ann)
    db.commit()
    return {"message": "Announcement deleted"}

@app.get("/admin/dogs")
def admin_list_dogs(db: Session = Depends(database.get_db), admin: models.User = Depends(require_admin)):
    from sqlalchemy import func
    dogs = db.query(models.Dog).all()
    breed_counts = db.query(models.Dog.breed, func.count(models.Dog.id)).group_by(models.Dog.breed).all()
    result = []
    for d in dogs:
        owner = db.query(models.User).filter(models.User.id == d.owner_id).first()
        health_count = db.query(models.HealthRecord).filter(models.HealthRecord.dog_id == d.id).count()
        result.append({
            "id": d.id, "name": d.name, "breed": d.breed, "color": d.color,
            "age": d.age, "weight": d.weight, "pet_type": d.pet_type,
            "owner_name": owner.full_name if owner else "Unknown",
            "health_records": health_count,
            "has_nose_print": d.nose_print_descriptor is not None
        })
    return {
        "dogs": result,
        "breed_distribution": {breed: count for breed, count in breed_counts},
        "total": len(dogs)
    }

# =====================================================
# Case Reporting API — Social Dog Case Reporting
# =====================================================

@app.post("/cases", response_model=schemas.CaseReportResponse)
def create_case_report(
    report: schemas.CaseReportCreate,
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(get_current_user)
):
    new_report = models.CaseReport(
        id=str(uuid.uuid4()),
        author_id=current_user.id,
        case_type=report.case_type,
        title=report.title,
        description=report.description,
        image_url=report.image_url,
        breed=report.breed,
        color=report.color,
        location=report.location,
        latitude=report.latitude,
        longitude=report.longitude,
        images=report.images,
    )
    db.add(new_report)
    db.commit()
    db.refresh(new_report)
    
    # Award Karma for reporting a case
    award_karma(db, current_user.id, 10, "case_report", f"Reported case: {new_report.title}")


    return {
        **{c.name: getattr(new_report, c.name) for c in new_report.__table__.columns},
        "author": {"id": current_user.id, "full_name": current_user.full_name, "profile_image": current_user.profile_image},
        "like_count": 0,
        "comment_count": 0,
        "is_liked": False,
    }


@app.get("/cases", response_model=List[schemas.CaseReportResponse])
def list_case_reports(
    skip: int = 0,
    limit: int = 50,
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(get_current_user)
):
    from sqlalchemy import func

    from sqlalchemy import or_
    
    # Filter: Show approved reports OR reports owned by the current user
    query = db.query(models.CaseReport).filter(
        or_(
            models.CaseReport.is_approved == True,
            models.CaseReport.author_id == current_user.id
        )
    )
    
    reports = query.order_by(models.CaseReport.created_at.desc()).offset(skip).limit(limit).all()

    results = []
    for r in reports:
        like_count = db.query(models.CaseLike).filter(models.CaseLike.report_id == r.id).count()
        comment_count = db.query(models.CaseComment).filter(models.CaseComment.report_id == r.id).count()
        is_liked = db.query(models.CaseLike).filter(
            models.CaseLike.report_id == r.id,
            models.CaseLike.user_id == current_user.id
        ).first() is not None

        author = db.query(models.User).filter(models.User.id == r.author_id).first()

        results.append({
            **{c.name: getattr(r, c.name) for c in r.__table__.columns},
            "author": {"id": author.id, "full_name": author.full_name, "profile_image": author.profile_image} if author else None,
            "like_count": like_count,
            "comment_count": comment_count,
            "is_liked": is_liked,
        })

    return results


@app.get("/cases/{report_id}", response_model=schemas.CaseReportResponse)
def get_case_report(
    report_id: str,
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(get_current_user)
):
    r = db.query(models.CaseReport).filter(models.CaseReport.id == report_id).first()
    if not r:
        raise HTTPException(status_code=404, detail="Case report not found")
        
    # Security: If not approved, only author or admin can view
    if not r.is_approved and r.author_id != current_user.id and current_user.role != models.UserRole.ADMIN:
        raise HTTPException(status_code=403, detail="Report pending moderation")

    like_count = db.query(models.CaseLike).filter(models.CaseLike.report_id == r.id).count()
    comment_count = db.query(models.CaseComment).filter(models.CaseComment.report_id == r.id).count()
    is_liked = db.query(models.CaseLike).filter(
        models.CaseLike.report_id == r.id,
        models.CaseLike.user_id == current_user.id
    ).first() is not None
    author = db.query(models.User).filter(models.User.id == r.author_id).first()

    return {
        **{c.name: getattr(r, c.name) for c in r.__table__.columns},
        "author": {"id": author.id, "full_name": author.full_name, "profile_image": author.profile_image} if author else None,
        "like_count": like_count,
        "comment_count": comment_count,
        "is_liked": is_liked,
    }


@app.post("/cases/{report_id}/comments", response_model=schemas.CaseCommentResponse)
def add_case_comment(
    report_id: str,
    comment: schemas.CaseCommentCreate,
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(get_current_user)
):
    report = db.query(models.CaseReport).filter(models.CaseReport.id == report_id).first()
    if not report:
        raise HTTPException(status_code=404, detail="Case report not found")

    new_comment = models.CaseComment(
        id=str(uuid.uuid4()),
        report_id=report_id,
        author_id=current_user.id,
        content=comment.content,
        tagged_users=comment.tagged_users,
    )
    db.add(new_comment)
    db.commit()
    db.refresh(new_comment)
    
    # Award Karma for commenting on a case
    award_karma(db, current_user.id, 2, "comment", f"Commented on case: {report.title}")


    return {
        **{c.name: getattr(new_comment, c.name) for c in new_comment.__table__.columns},
        "author": {"id": current_user.id, "full_name": current_user.full_name, "profile_image": current_user.profile_image},
    }


@app.get("/cases/{report_id}/comments", response_model=List[schemas.CaseCommentResponse])
def list_case_comments(
    report_id: str,
    db: Session = Depends(database.get_db),
):
    comments = db.query(models.CaseComment).filter(
        models.CaseComment.report_id == report_id
    ).order_by(models.CaseComment.created_at.asc()).all()

    results = []
    for c in comments:
        author = db.query(models.User).filter(models.User.id == c.author_id).first()
        results.append({
            **{col.name: getattr(c, col.name) for col in c.__table__.columns},
            "author": {"id": author.id, "full_name": author.full_name, "profile_image": author.profile_image} if author else None,
        })
    return results


@app.post("/cases/{report_id}/like")
def toggle_case_like(
    report_id: str,
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(get_current_user)
):
    report = db.query(models.CaseReport).filter(models.CaseReport.id == report_id).first()
    if not report:
        raise HTTPException(status_code=404, detail="Case report not found")

    existing = db.query(models.CaseLike).filter(
        models.CaseLike.report_id == report_id,
        models.CaseLike.user_id == current_user.id
    ).first()

    if existing:
        db.delete(existing)
        db.commit()
        return {"liked": False}
    else:
        new_like = models.CaseLike(
            id=str(uuid.uuid4()),
            report_id=report_id,
            user_id=current_user.id,
        )
        db.add(new_like)
        db.commit()
        return {"liked": True}


@app.get("/users/search")
def search_users(
    q: str,
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(get_current_user)
):
    """Search users by name for @mention tagging"""
    users = db.query(models.User).filter(
        models.User.full_name.ilike(f"%{q}%")
    ).limit(10).all()
    return [
        {"id": u.id, "full_name": u.full_name, "profile_image": u.profile_image}
        for u in users
    ]

@app.post("/payments/initiate")
async def initiate_payment(order_id: str, amount: float, email: str, phone: str = "0700000000"):
    # 1. Register IPN
    ipn_res = pesapal.register_ipn(os.getenv("PESAPAL_IPN_URL"))
    ipn_id = ipn_res.get("ipn_id")
    
    if not ipn_id:
        raise HTTPException(status_code=500, detail="Failed to register IPN with Pesapal")
        
    # 2. Submit Order
    callback_url = os.getenv("PESAPAL_CALLBACK_URL")
    order_res = pesapal.submit_order(
        order_id=order_id,
        amount=amount,
        description=f"Lovedogs 360 - Order {order_id}",
        email=email,
        phone=phone,
        callback_url=callback_url,
        ipn_id=ipn_id
    )
    
    return order_res

@app.get("/pesapal/callback")
async def pesapal_callback(OrderTrackingId: str, OrderMerchantReference: str, db: Session = Depends(database.get_db)):
    # Verify status
    status_res = pesapal.get_transaction_status(OrderTrackingId)
    # Update order in DB
    return {"status": "processed", "data": status_res}

@app.get("/pesapal/ipn")
async def pesapal_ipn(OrderTrackingId: str, OrderMerchantReference: str, OrderNotificationType: str):
    # Log IPN
    logger.info(f"IPN Received: {OrderTrackingId} for Order {OrderMerchantReference}")
    return {"status": "acknowledged"}
# =====================================================
# Community Hub & Social Endpoints
# =====================================================

import re
from collections import Counter
from sqlalchemy import String

@app.get("/chat/global", response_model=List[schemas.CommunityMessageResponse])
def get_global_chat(tag: Optional[str] = None, db: Session = Depends(database.get_db), current_user: models.User = Depends(get_current_user)):
    query = db.query(models.CommunityMessage).filter(
        models.CommunityMessage.is_global == True,
        models.CommunityMessage.is_hidden == False
    )
    if tag:
        query = query.filter(models.CommunityMessage.hashtags.cast(String).ilike(f'%"{tag}"%'))
        
    messages = query.order_by(models.CommunityMessage.created_at.desc()).limit(50).all()
    for msg in messages:
        if msg.is_poll and msg.poll_options:
            votes = db.query(models.CommunityPollVote).filter(models.CommunityPollVote.message_id == msg.id).all()
            results = {str(opt["id"]): 0 for opt in msg.poll_options}
            current_user_vote = None
            for v in votes:
                results[str(v.option_id)] = results.get(str(v.option_id), 0) + 1
                if v.user_id == current_user.id:
                    current_user_vote = v.option_id
            msg.poll_results = results
            msg.has_voted = current_user_vote
    return messages

@app.get("/chat/nearby", response_model=List[schemas.CommunityMessageResponse])
def get_nearby_chat(radius: float = 20.0, db: Session = Depends(database.get_db), current_user: models.User = Depends(get_current_user)):
    # User's registered location
    u_lat, u_lon = current_user.latitude, current_user.longitude
    if u_lat is None or u_lon is None:
        return []

    # Get all messages with lat/lng and filter manually (or use PostGIS if available, but Haversine is fallback)
    messages = db.query(models.CommunityMessage).filter(models.CommunityMessage.latitude != None).all()
    nearby = []
    for msg in messages:
        dist = calculate_distance(u_lat, u_lon, msg.latitude, msg.longitude)
        if dist <= radius:
            nearby.append(msg)
    
    return sorted(nearby, key=lambda x: x.created_at, reverse=True)[:50]

@app.post("/chat/message", response_model=schemas.CommunityMessageResponse)
def post_community_message(msg: schemas.CommunityMessageCreate, db: Session = Depends(database.get_db), current_user: models.User = Depends(get_current_user)):
    hashtags = list(set([word.lower() for word in re.findall(r'#(\\w+)', msg.content)]))
    if msg.hashtags: # Also allow explicitly passed tags
        hashtags = list(set(hashtags + [h.lower().strip('#') for h in msg.hashtags]))
        
    new_msg = models.CommunityMessage(
        id=str(uuid.uuid4()),
        author_id=current_user.id,
        content=msg.content,
        latitude=current_user.latitude,
        longitude=current_user.longitude,
        is_global=msg.is_global,
        reshare_id=msg.reshare_id,
        hashtags=hashtags,
        is_poll=msg.is_poll,
        poll_options=msg.poll_options
    )
    db.add(new_msg)
    
    # Award Karma for participating
    award_karma(db, current_user.id, 1, "chat", "Sent a community message")
    
    db.commit()
    db.refresh(new_msg)
    
    if new_msg.is_poll:
        new_msg.poll_results = {str(opt["id"]): 0 for opt in (new_msg.poll_options or [])}
        new_msg.has_voted = None
        
    return new_msg

@app.get("/chat/trending-tags", response_model=List[schemas.TrendingTagResponse])
def get_trending_tags(db: Session = Depends(database.get_db)):
    seven_days_ago = datetime.utcnow() - timedelta(days=7)
    msgs = db.query(models.CommunityMessage).filter(
        models.CommunityMessage.created_at >= seven_days_ago,
        models.CommunityMessage.is_hidden == False
    ).all()
    
    all_tags = []
    for m in msgs:
        if m.hashtags:
            all_tags.extend(m.hashtags)
            
    counter = Counter(all_tags)
    return [{"tag": tag, "count": count} for tag, count in counter.most_common(10)]

@app.post("/chat/messages/{message_id}/vote")
def vote_poll(message_id: str, vote: schemas.PollVoteCreate, db: Session = Depends(database.get_db), current_user: models.User = Depends(get_current_user)):
    msg = db.query(models.CommunityMessage).filter(models.CommunityMessage.id == message_id).first()
    if not msg or not msg.is_poll:
        raise HTTPException(status_code=404, detail="Poll not found")
        
    existing = db.query(models.CommunityPollVote).filter(
        models.CommunityPollVote.message_id == message_id,
        models.CommunityPollVote.user_id == current_user.id
    ).first()
    
    if existing:
        existing.option_id = vote.option_id
    else:
        new_vote = models.CommunityPollVote(
            id=str(uuid.uuid4()),
            message_id=message_id,
            user_id=current_user.id,
            option_id=vote.option_id
        )
        db.add(new_vote)
    db.commit()
    return {"status": "success"}

@app.post("/chat/messages/{message_id}/flag")
def flag_message(message_id: str, db: Session = Depends(database.get_db), current_user: models.User = Depends(get_current_user)):
    msg = db.query(models.CommunityMessage).filter(models.CommunityMessage.id == message_id).first()
    if not msg:
        raise HTTPException(status_code=404, detail="Message not found")
        
    msg.flag_count += 1
    if msg.flag_count >= 3:
        msg.is_hidden = True
        
    db.commit()
    return {"status": "success", "is_hidden": msg.is_hidden}

@app.post("/chat/messages/{message_id}/react")
def react_to_message(message_id: str, reaction: schemas.ChatReactionCreate, db: Session = Depends(database.get_db), current_user: models.User = Depends(get_current_user)):
    # Check if existing reaction
    existing = db.query(models.ChatReaction).filter(
        models.ChatReaction.message_id == message_id,
        models.ChatReaction.user_id == current_user.id
    ).first()
    
    if existing:
        existing.reaction_type = reaction.reaction_type
    else:
        new_react = models.ChatReaction(
            id=str(uuid.uuid4()),
            message_id=message_id,
            user_id=current_user.id,
            reaction_type=reaction.reaction_type
        )
        db.add(new_react)
        
    db.commit()
    return {"status": "success"}

@app.get("/chat/dms", response_model=List[schemas.DirectMessageResponse])
def get_my_dms(db: Session = Depends(database.get_db), current_user: models.User = Depends(get_current_user)):
    return db.query(models.DirectMessage).filter(
        (models.DirectMessage.sender_id == current_user.id) | 
        (models.DirectMessage.receiver_id == current_user.id)
    ).order_by(models.DirectMessage.created_at.desc()).all()

@app.post("/chat/dm", response_model=schemas.DirectMessageResponse)
def send_direct_message(dm: schemas.DirectMessageCreate, db: Session = Depends(database.get_db), current_user: models.User = Depends(get_current_user)):
    new_dm = models.DirectMessage(
        id=str(uuid.uuid4()),
        sender_id=current_user.id,
        receiver_id=dm.receiver_id,
        content=dm.content
    )
    db.add(new_dm)
    db.commit()
    db.refresh(new_dm)
    return new_dm

# =====================================================
# Karma & Status Endpoints
# =====================================================

@app.post("/users/status/heartbeat")
def user_heartbeat(db: Session = Depends(database.get_db), current_user: models.User = Depends(get_current_user)):
    current_user.is_online = True
    current_user.last_seen = datetime.utcnow()
    db.commit()
    return {"status": "online"}

@app.get("/users/online", response_model=List[schemas.UserResponse])
def get_online_users(db: Session = Depends(database.get_db)):
    # Simple logic: seen in the last 5 minutes
    five_mins_ago = datetime.utcnow() - timedelta(minutes=5)
    return db.query(models.User).filter(models.User.last_seen >= five_mins_ago).all()

@app.post("/karma/redeem")
def redeem_karma(req: schemas.KarmaRedeemRequest, db: Session = Depends(database.get_db), current_user: models.User = Depends(get_current_user)):
    if current_user.available_karma < req.amount_to_redeem:
        raise HTTPException(status_code=400, detail="Insufficient karma points")
    
    current_user.available_karma -= req.amount_to_redeem
    
    # Track redemption
    transaction = models.KarmaTransaction(
        id=str(uuid.uuid4()),
        user_id=current_user.id,
        amount=-req.amount_to_redeem,
        category="redemption",
        description=f"Redeemed {req.amount_to_redeem} points for marketplace credit"
    )
    db.add(transaction)
    db.commit()
    
    return {"status": "success", "new_balance": current_user.available_karma}

# =====================================================
# Support Tickets & Announcements (Admin)
# =====================================================



@app.get("/admin/support-tickets")
def get_all_support_tickets(
    db: Session = Depends(database.get_db),
    admin: models.User = Depends(require_admin)
):
    tickets = db.query(models.SupportTicket).order_by(models.SupportTicket.created_at.desc()).all()
    results = []
    for t in tickets:
        u = db.query(models.User).filter(models.User.id == t.user_id).first()
        results.append({
            "id": t.id, "subject": t.subject, "message": t.message, "status": t.status,
            "user_name": u.full_name if u else "Unknown", "created_at": str(t.created_at)
        })
    return results

class AdminSupportReplyMsg(BaseModel):
    message: str

@app.post("/admin/support-tickets/{ticket_id}/reply")
def reply_support_ticket(
    ticket_id: str,
    req: AdminSupportReplyMsg,
    db: Session = Depends(database.get_db),
    admin: models.User = Depends(require_admin)
):
    ticket = db.query(models.SupportTicket).filter(models.SupportTicket.id == ticket_id).first()
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found")
    
    ticket.admin_reply = req.message
    ticket.status = "In-Progress"
    db.commit()
    return {"message": "Reply sent"}

@app.post("/admin/support-tickets/{ticket_id}/resolve")
def resolve_support_ticket(
    ticket_id: str,
    db: Session = Depends(database.get_db),
    admin: models.User = Depends(require_admin)
):
    ticket = db.query(models.SupportTicket).filter(models.SupportTicket.id == ticket_id).first()
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found")
    
    ticket.status = "Resolved"
    db.commit()
    return {"message": "Ticket resolved"}

@app.post("/admin/announcements", response_model=schemas.AnnouncementResponse)
def create_announcement(
    req: schemas.AnnouncementCreate,
    db: Session = Depends(database.get_db),
    admin: models.User = Depends(require_admin)
):
    announcement = models.Announcement(
        id=str(uuid.uuid4()),
        title=req.title,
        message=req.message,
        target_audience=req.target_audience
    )
    db.add(announcement)
    db.commit()
    db.refresh(announcement)
    return announcement

@app.get("/announcements", response_model=List[schemas.AnnouncementResponse])
def get_announcements(
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(get_current_user)
):
    # Depending on user role, filter target_audience
    if current_user.role == "buyer":
        return db.query(models.Announcement).filter(models.Announcement.target_audience.in_(["all", "buyers"])).order_by(models.Announcement.created_at.desc()).all()
    elif current_user.role == "provider":
        return db.query(models.Announcement).filter(models.Announcement.target_audience.in_(["all", "providers"])).order_by(models.Announcement.created_at.desc()).all()
    else:
        return db.query(models.Announcement).order_by(models.Announcement.created_at.desc()).all()

# --- Personalized Notifications ---

def create_notification(db: Session, user_id: str, title: str, message: str, type: str = "info"):
    new_notif = models.Notification(
        id=str(uuid.uuid4()),
        user_id=user_id,
        title=title,
        message=message,
        type=type
    )
    db.add(new_notif)
    db.commit()
    return new_notif

@app.get("/notifications", response_model=List[schemas.NotificationResponse])
def get_notifications(
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(get_current_user)
):
    return db.query(models.Notification).filter(
        models.Notification.user_id == current_user.id
    ).order_by(models.Notification.created_at.desc()).limit(50).all()

@app.post("/notifications/{notification_id}/read")
def mark_notification_read(
    notification_id: str,
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(get_current_user)
):
    notif = db.query(models.Notification).filter(
        models.Notification.id == notification_id,
        models.Notification.user_id == current_user.id
    ).first()
    if notif:
        notif.is_read = True
        db.commit()
    return {"message": "Success"}

# =====================================================
# QR Code Ticket Verification (Admin)
# =====================================================

@app.get("/admin/verify-ticket")
def verify_event_ticket(
    token: str,
    db: Session = Depends(database.get_db),
    admin: models.User = Depends(require_admin)
):
    registration = db.query(models.Registration).filter(models.Registration.ticket_token == token).first()
    if not registration:
        raise HTTPException(status_code=404, detail="Ticket not found or invalid token")
    
    user = db.query(models.User).filter(models.User.id == registration.user_id).first()
    event = db.query(models.Event).filter(models.Event.id == registration.event_id).first()
    
    if not user or not event:
        raise HTTPException(status_code=404, detail="Data inconsistency found for ticket")
        
    return {
        "valid": True,
        "checked_in": registration.check_in_time is not None,
        "check_in_time": registration.check_in_time,
        "registration_status": registration.status,
        "user_name": user.full_name,
        "user_email": user.email,
        "event_title": event.title,
        "role": registration.role
    }

@app.post("/admin/check-in-ticket")
def check_in_ticket(
    token: str,
    db: Session = Depends(database.get_db),
    admin: models.User = Depends(require_admin)
):
    registration = db.query(models.Registration).filter(models.Registration.ticket_token == token).first()
    if not registration:
        raise HTTPException(status_code=404, detail="Ticket not found or invalid token")
    
    if registration.check_in_time is not None:
        raise HTTPException(status_code=400, detail="Ticket has already been used")
        
    registration.check_in_time = datetime.utcnow()
    registration.status = "checked-in"
    db.commit()
    
    return {"message": "Success", "checked_in": True, "time": registration.check_in_time}

# =====================================================
# Admin User Management
# =====================================================

@app.post("/admin/users", response_model=schemas.UserResponse)
def admin_create_user(
    req: schemas.UserCreate,
    db: Session = Depends(database.get_db),
    admin: models.User = Depends(require_admin)
):
    existing = db.query(models.User).filter(models.User.email == req.email).first()
    if existing:
        raise HTTPException(status_code=400, detail="Email already registered")
        
    user = models.User(
        id=str(uuid.uuid4()),
        email=req.email,
        full_name=req.full_name,
        role=req.role or "buyer",
        phone_number=req.phone_number,
        hashed_password=auth.get_password_hash(req.password)
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user

@app.post("/admin/users/{user_id}/suspend")
def admin_suspend_user(
    user_id: str,
    db: Session = Depends(database.get_db),
    admin: models.User = Depends(require_admin)
):
    user = db.query(models.User).filter(models.User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
        
    user.role = "suspended"
    db.commit()
    return {"message": f"User {user.email} suspended"}

# --- Support Tickets API ---

@app.post("/support", response_model=schemas.SupportTicketResponse)
def create_support_ticket(
    ticket: schemas.SupportTicketCreate,
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(get_current_user)
):
    new_ticket = models.SupportTicket(
        id=str(uuid.uuid4()),
        user_id=current_user.id,
        subject=ticket.subject,
        message=ticket.message,
        images=ticket.images
    )
    db.add(new_ticket)
    db.commit()
    db.refresh(new_ticket)
    return new_ticket

@app.get("/support", response_model=List[schemas.SupportTicketResponse])
def get_support_tickets(
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(get_current_user)
):
    if current_user.role in ["admin", "super_admin"]:
        tickets = db.query(models.SupportTicket).order_by(models.SupportTicket.created_at.desc()).all()
    else:
        tickets = db.query(models.SupportTicket).filter(models.SupportTicket.user_id == current_user.id).order_by(models.SupportTicket.created_at.desc()).all()
    return tickets

class SupportReply(BaseModel):
    admin_reply: str

@app.post("/support/{ticket_id}/reply")
def reply_to_support_ticket(
    ticket_id: str,
    reply: SupportReply,
    db: Session = Depends(database.get_db),
    admin: models.User = Depends(require_admin)
):
    ticket = db.query(models.SupportTicket).filter(models.SupportTicket.id == ticket_id).first()
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found")
    
    ticket.admin_reply = reply.admin_reply
    ticket.status = "resolved"
    db.commit()
    
    # Notify User
    create_notification(
        db, 
        ticket.user_id, 
        "Support Ticket Reply", 
        f"An admin has replied to your ticket: '{ticket.subject}'. Feedback: {reply.admin_reply}",
        "feedback"
    )

    db.refresh(ticket)
    return {"message": "Reply sent successfully", "status": ticket.status}



exchange_rates_cache = {
    "rates": {},
    "last_updated": 0
}

@app.get("/exchange-rates")
def get_exchange_rates():
    current_time = time.time()
    # Cache for 1 hour (3600 seconds)
    if current_time - exchange_rates_cache["last_updated"] > 3600 or not exchange_rates_cache["rates"]:
        try:
            req = urllib.request.Request("https://open.er-api.com/v6/latest/USD")
            with urllib.request.urlopen(req, timeout=5) as response:
                if response.status == 200:
                    data = json.loads(response.read().decode())
                    exchange_rates_cache["rates"] = data.get("rates", {})
                    exchange_rates_cache["last_updated"] = current_time
        except Exception as e:
            logger.warning(f"Error fetching exchange rates: {e}")
            # Silently fail and return stale rules if any exist
    return {"rates": exchange_rates_cache["rates"]}


# =====================================================
# User Safety — Report & Block (Store Compliance)
# =====================================================

class CaseFlagRequest(BaseModel):
    reason: str  # spam, harmful, misinformation

@app.post("/cases/{case_id}/flag")
async def flag_case_report(
    case_id: str,
    flag: CaseFlagRequest,
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(get_current_user)
):
    """
    Allows a user to report/flag a community case post for moderation review.
    Stores an audit log entry visible in the admin moderation panel.
    Does NOT delete the post — admin reviews first.
    """
    case = db.query(models.CaseReport).filter(models.CaseReport.id == case_id).first()
    if not case:
        raise HTTPException(status_code=404, detail="Post not found")

    # Prevent self-reporting
    if case.author_id == current_user.id:
        raise HTTPException(status_code=400, detail="You cannot report your own post")

    # Check for duplicate report from same user
    existing = db.query(models.AuditLog).filter(
        models.AuditLog.user_id == current_user.id,
        models.AuditLog.action == "flag_case",
        models.AuditLog.target_id == case_id
    ).first()
    if existing:
        return {"message": "You have already reported this post"}

    # Record the report in AuditLog (reuses existing model — no migration needed)
    log = models.AuditLog(
        id=str(uuid.uuid4()),
        user_id=current_user.id,
        action="flag_case",
        target_type="case_report",
        target_id=case_id,
        details=f"reason={flag.reason}"
    )
    db.add(log)
    db.commit()

    logger.info(f"Case {case_id} flagged by user {current_user.id} for reason: {flag.reason}")
    return {"message": "Report submitted successfully. Our moderation team will review this post."}


@app.post("/users/{user_id}/block")
async def block_user(
    user_id: str,
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(get_current_user)
):
    """
    Allows a user to block another user.
    Stores a block record in AuditLog for admin visibility.
    Blocked state is tracked client-side; blocking does not remove content.
    """
    if user_id == current_user.id:
        raise HTTPException(status_code=400, detail="You cannot block yourself")

    target_user = db.query(models.User).filter(models.User.id == user_id).first()
    if not target_user:
        raise HTTPException(status_code=404, detail="User not found")

    # Check if already blocked
    existing_block = db.query(models.AuditLog).filter(
        models.AuditLog.user_id == current_user.id,
        models.AuditLog.action == "block_user",
        models.AuditLog.target_id == user_id
    ).first()
    if existing_block:
        return {"message": "User is already blocked", "blocked": True}

    # Record block in AuditLog
    log = models.AuditLog(
        id=str(uuid.uuid4()),
        user_id=current_user.id,
        action="block_user",
        target_type="user",
        target_id=user_id,
        details=f"blocker={current_user.email} blocked={target_user.email}"
    )
    db.add(log)
    db.commit()

    logger.info(f"User {current_user.id} blocked user {user_id}")
    return {"message": f"User has been blocked successfully", "blocked": True}


from fastapi import FastAPI

@app.get("/health")
async def health():
    return {"status": "ok"}

from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
import os

# Serve static assets
if os.path.exists("static"):
    app.mount("/_expo", StaticFiles(directory="static/_expo"), name="expo")
app.mount("/assets", StaticFiles(directory="static/assets"), name="assets")

# Serve frontend for ALL unmatched routes including /
@app.get("/{full_path:path}", include_in_schema=False)
async def serve_frontend(full_path: str):
    import os
    # Try to serve actual static file first
    file_path = os.path.join("static", full_path)
    if os.path.isfile(file_path):
        return FileResponse(file_path)
    return FileResponse("static/index.html")
