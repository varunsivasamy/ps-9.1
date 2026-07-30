"""AWS Lambda entry point: wraps the FastAPI app with Mangum.

infra/template.yaml points the function's handler at
``autonomy_engine.lambda_handler.handler``.
"""

from mangum import Mangum

from autonomy_engine.main import app

handler = Mangum(app)
