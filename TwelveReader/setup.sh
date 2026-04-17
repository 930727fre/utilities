#!/usr/bin/env bash
set -e

echo "==> Cloning foliate-js..."
mkdir -p frontend/public/foliate-js
git clone --depth 1 https://github.com/johnfactotum/foliate-js.git frontend/public/foliate-js

echo "==> Installing frontend dependencies..."
cd frontend && npm install && cd ..

echo "==> Creating data directories..."
mkdir -p data/books data/cache

echo ""
echo "Setup complete."
echo ""
echo "To run locally (dev mode):"
echo "  Backend:  cd backend && uvicorn main:app --reload"
echo "  Frontend: cd frontend && npm run dev"
echo ""
echo "To run with Docker Compose:"
echo "  docker compose up --build"
