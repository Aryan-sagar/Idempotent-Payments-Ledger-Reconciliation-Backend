import redis

from app.config import settings

# decode_responses=True so we get str back instead of bytes everywhere else in the app
redis_client = redis.Redis.from_url(settings.redis_url, decode_responses=True)
