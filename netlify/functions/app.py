from mangum import Mangum

from api.main import app

# Netlify Functions run on AWS Lambda-compatible runtime.
handler = Mangum(app, lifespan="off")
