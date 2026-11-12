# firecrawl

the worked example of a toolbox entry, kept small on purpose so the shape is legible.

nothing is baked in and nothing is copied, which is what makes it toolbox rather than
ingredient: the project is agpl-3.0, and copying it into an apache-2.0 backend is exactly
the thing that licence forbids. an agent asks for a page, the platform posts to your key,
and the result is a plain dict.

```sh
CRITTERTOWN_FIRECRAWL_API_KEY=fc-...
CRITTERTOWN_FIRECRAWL_BASE_URL=https://api.firecrawl.dev/v1   # or your own deployment
```

three calls, and they are all an agent needs: `scrape` one page to markdown, `crawl` a site
to a bounded page count, `map` a site to its urls without fetching them. a job that answers
with an id instead of a result is reported as `pending`, so the caller knows to ask again
rather than being told the crawl is empty.

`client.py` is the pattern for the next one: `configured()`, `status()` that says what is
missing, one method per call returning a plain dict, `ProviderFailure` on anything that is
not 2xx, and an `httpx` transport argument so the suite can drive it without a network.
