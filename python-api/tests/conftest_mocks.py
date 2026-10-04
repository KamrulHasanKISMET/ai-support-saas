"""
Mock setup for running Language Intelligence tests outside Docker.
Import this at the top of test runner to stub external packages.
"""
import sys
from unittest.mock import MagicMock, AsyncMock

def setup_mocks():
    # sqlalchemy mocks
    sa_mock = MagicMock()
    sa_mock.text = lambda q: q  # text() just returns the string
    sys.modules['sqlalchemy'] = sa_mock
    sys.modules['sqlalchemy.ext'] = MagicMock()
    sys.modules['sqlalchemy.ext.asyncio'] = MagicMock()
    sys.modules['sqlalchemy.orm'] = MagicMock()

    # app internals that tests don't exercise directly
    for mod in [
        'app.core.config', 'app.core.error_types', 'app.core.trace_sanitize',
        'app.ai.ai_service', 'app.ai.embedding_service',
        'app.intent.intent_engine',
        'app.language.language_engine', 'app.language.language_types',
        'app.memory.memory_service', 'app.state.state_engine',
        'app.context.context_engine', 'app.trace.trace_service',
        'app.trace.trace_types', 'app.agent.agent_config',
        'app.schemas.agent', 'app.schemas.kernel',
    ]:
        sys.modules[mod] = MagicMock()

    # app.core.logging — must return a real logger-like object
    import logging
    log_mod = MagicMock()
    log_mod.logger = logging.getLogger("test")
    sys.modules['app.core.logging'] = log_mod

    # IntentType needs real values for routing/kernel tests
    from enum import Enum
    class IntentType(Enum):
        ORDER_STATUS = "ORDER_STATUS"
        CREATE_ORDER = "CREATE_ORDER"
        PRICE_INQUIRY = "PRICE_INQUIRY"
        PRODUCT_INFO = "PRODUCT_INFO"
        PRODUCT_AVAILABILITY = "PRODUCT_AVAILABILITY"
        DELIVERY_INFO = "DELIVERY_INFO"
        RETURN_REQUEST = "RETURN_REQUEST"
        COMPLAINT = "COMPLAINT"
        NEGOTIATION = "NEGOTIATION"
        GENERAL_QUESTION = "GENERAL_QUESTION"

    intent_mod = MagicMock()
    intent_mod.IntentType = IntentType
    sys.modules['app.intent.intent_types'] = intent_mod

setup_mocks()
