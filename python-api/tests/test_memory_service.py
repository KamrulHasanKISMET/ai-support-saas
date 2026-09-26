import unittest
from unittest.mock import AsyncMock, patch

from app.memory.memory_service import memory_service


class TestMemoryServiceTokenUsage(unittest.IsolatedAsyncioTestCase):
    async def test_usage_out_is_forwarded_to_ai_service(self):
        usage_out = {}

        with patch(
            "app.memory.memory_service.ai_service.complete_json",
            new=AsyncMock(
                return_value={
                    "memories": [],
                }
            ),
        ) as mocked_complete_json:
            await memory_service.extract_and_store(
                db=AsyncMock(),
                tenant_id=1,
                customer_id=2,
                message="My favorite color is blue.",
                usage_out=usage_out,
            )

        mocked_complete_json.assert_awaited_once()
        self.assertIs(
            mocked_complete_json.await_args.kwargs["usage_out"],
            usage_out,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
