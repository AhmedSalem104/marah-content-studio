# Future Notion API mirror

This implementation is prepared but inactive. No integration credential is available, and the workflow is a template outside `.github/workflows`. The initial Notion export remains separate from `content/notion/api-current`.

Create a read-only Notion integration, grant it access to root page `3e43ab32c440810e99edc6c7737c3119` and database `d49a1dc094ae49819be1d46059ef3821`, and provide its secret through `NOTION_TOKEN`. From the repository root run:

```sh
python -m unittest discover -s tests -p 'test_sync_notion.py'
python automation/scripts/sync_notion.py
```

For future scheduled execution, configure the repository secret and copy `automation/workflows/notion-sync.yml.template` into `.github/workflows/notion-sync.yml`. The template runs manually or hourly, serializes executions, and commits only the scoped mirror directory. It does not publish videos.

The pinned API version is `2025-09-03`. The mirror follows child pages, child databases and the explicitly configured database; queries every database data source; recursively paginates blocks and page properties; and stores each API response separately. It does not search the workspace or follow linked/referenced pages. References are reported in the manifest. Raw JSON retains API fields except hosted file URLs and expiry values, which are replaced by local asset paths. Volatile request IDs and personal email fields are removed; a manifest records file hashes and sizes. External URLs stay links.

Hosted files in nested page/block objects, icons, covers and properties are downloaded without the Notion authorization header. Downloads require public HTTPS addresses on recognized Notion/AWS hosted domains, revalidate redirect destinations, have a 30-second timeout, and are limited to 19 MiB. Oversized media must move to a separately designed media storage pipeline. A failed asset or API request aborts the snapshot; bounded 429/5xx retries and API pacing stay below three requests per second. Errors do not print tokens or signed URLs.

The mirror builds a temporary complete snapshot before replacing the current directory. Failure during retrieval leaves the previous snapshot untouched; failed replacement restores the previous directory. A process interruption during replacement can leave `.api-current-previous`; inspect and restore it before another run. Git retains committed snapshot history. Concurrent local runs are unsupported; workflow concurrency prevents overlapping scheduled jobs.

The manifest deliberately marks the export partial: this is scoped API-visible content, not a workspace backup. Comments, version history, inaccessible content and non-API features are excluded. Unsupported blocks remain API objects and are listed. There is no claim of full Notion fidelity or arbitrary external-asset capture.

Official API references: [block children](https://developers.notion.com/reference/get-block-children), [database data sources](https://developers.notion.com/reference/database), [query a data source](https://developers.notion.com/reference/query-a-data-source), [page property items](https://developers.notion.com/reference/retrieve-a-page-property).
