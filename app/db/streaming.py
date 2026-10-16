"""Fetch corpus rows in bounded network batches within the caller's transaction."""


async def fetch_rows(session, statement, params=None, *, batch_size=500, scalars=False, mappings=False):
    result = await session.stream(statement, params, execution_options={"yield_per": batch_size})
    try:
        values = result.scalars() if scalars else result.mappings() if mappings else result
        return [row async for row in values]
    finally:
        await result.close()
