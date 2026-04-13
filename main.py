import logging

from fastapi import FastAPI

from database import Base, engine
from routers.posts import router as posts_router
from routers.posts import tags_router
from routers.scrape import router as scrape_router

logging.basicConfig(level=logging.INFO)

# Create all tables on startup (idempotent)
Base.metadata.create_all(bind=engine)

app = FastAPI(
    title="JeevesKitchen",
    description="Search and archive tool for the Ecosophia community blog.",
    version="0.1.0",
)

app.include_router(scrape_router)
app.include_router(posts_router)
app.include_router(tags_router)


@app.get("/")
def root():
    return {"message": "Jeeves backend is running"}