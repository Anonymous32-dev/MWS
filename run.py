import os
import uvicorn

if __name__ == "__main__":
    port = int(os.getenv("SERVER_PORT", os.getenv("PORT", 10507)))
    uvicorn.run("backend.main:app", host="0.0.0.0", port=port, reload=False)