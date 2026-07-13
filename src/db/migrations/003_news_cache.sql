-- news_cache table for RSS feed aggregation
-- Stores fetched articles with ticker extraction and keyword sentiment.
-- URI-unique constraint enables idempotent upserts.
-- Array columns + GIN index support fast agent queries.

CREATE TABLE IF NOT EXISTS public.news_cache (
    id SERIAL PRIMARY KEY,
    url TEXT UNIQUE NOT NULL,
    title TEXT NOT NULL,
    summary TEXT,
    source TEXT NOT NULL,  -- 'marketwatch', 'yahoo', 'bloomberg', 'cnbc', 'seekingalpha'
    published_at TIMESTAMPTZ NOT NULL,
    collected_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    tickers TEXT[],  -- extracted ticker symbols
    sentiment_score FLOAT DEFAULT 0.0,
    full_text TEXT  -- full article text if available
);

CREATE INDEX IF NOT EXISTS idx_news_cache_published ON public.news_cache(published_at DESC);
CREATE INDEX IF NOT EXISTS idx_news_cache_tickers ON public.news_cache USING GIN(tickers);
CREATE INDEX IF NOT EXISTS idx_news_cache_source ON public.news_cache(source);