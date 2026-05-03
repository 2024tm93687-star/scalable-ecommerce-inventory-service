# Inventory Service

ECI Inventory microservice for multi-warehouse stock, reservation, release, and ship movements.

## Features
- Versioned API: `/v1/inventory`
- Atomic reservation with idempotency
- Warehouse allocation (single-warehouse preferred, otherwise split)
- Reservation TTL cleanup reaper
- Movement audit APIs
- Low-stock tracking metric

## Quick Start

### Option 1: Local Development (No Docker)
1. Ensure PostgreSQL is running and `inventory_db` exists.
2. Create `.env` from `.env.example`.
3. Run:
   ```bash
   npm install
   npm run seed  # optional
   npm start
   ```
4. Service runs on `http://localhost:3002`

### Option 2: Docker (Single Service)
1. Build the Docker image:
   ```bash
   docker build -t eci-inventory-service:latest .
   ```
2. Create Docker network (if not exists):
   ```bash
   docker network create eci-net
   ```
3. Run PostgreSQL container:
   ```bash
   docker run -d --name inventory-db --network eci-net \
     -e POSTGRES_USER=user \
     -e POSTGRES_PASSWORD=password \
     -e POSTGRES_DB=inventory_db \
     -p 5432:5432 \
     postgres:16-alpine
   ```
4. Run the service container:
   ```bash
   docker run -d --name inventory-service --network eci-net \
     -e DATABASE_URL=postgres://user:password@inventory-db:5432/inventory_db \
     -e APP_PORT=3002 \
     -p 3002:3002 \
     eci-inventory-service:latest
   ```
5. Verify running:
   ```bash
   curl http://localhost:3002/health
   ```

### Option 3: Docker Compose (Full Stack - from root directory)
From the `FullApplication/` root directory:
```bash
# Build all services and start the stack
docker compose -f docker-compose.yml up --build -d

# View logs
docker compose -f docker-compose.yml logs -f inventory-service

# Stop all services
docker compose -f docker-compose.yml down
```

### Seeding (PowerShell)
Run from the `FullApplication/` root directory:
```powershell
# Seed only inventory service
docker compose -f docker-compose.yml exec inventory-service npm run seed
```

## Core Endpoints
- `GET /health` — Health check
- `GET /v1/inventory` — List inventory
- `POST /v1/inventory/reserve` — Reserve stock (idempotent)
- `POST /v1/inventory/release` — Release reserved stock
- `POST /v1/inventory/ship` — Ship items (decrement on_hand & reserved)
- `GET /v1/inventory/movements` — Audit trail of movements
- `GET /docs` — OpenAPI Swagger UI
- `GET /metrics` — Prometheus metrics

## Kubernetes Deployment (Minikube)

### Prerequisites
- Minikube running: `minikube start`
- kubectl configured
- Image available in Minikube

### Deployment Steps

1. **Build image for Minikube**:
   ```bash
   eval $(minikube docker-env)
   docker build -t eci-inventory-service:latest .
   ```

2. **Apply Kubernetes manifests** (from service root):
   ```bash
   kubectl apply -f k8s/inventory-config.yaml
   kubectl apply -f k8s/inventory-db.yaml
   kubectl rollout status statefulset/inventory-db
   kubectl apply -f k8s/inventory-service.yaml
   kubectl rollout status deployment/inventory-service
   ```

3. **Verify deployment**:
   ```bash
   kubectl get pods -l app=inventory-service
   kubectl get svc inventory-service
   kubectl logs -l app=inventory-service -f
   ```

4. **Access the service** (port-forward):
   ```bash
   kubectl port-forward svc/inventory-service 3002:3002
   curl http://localhost:3002/health
   ```

5. **Cleanup**:
   ```bash
   kubectl delete -f k8s/inventory-service.yaml
   kubectl delete -f k8s/inventory-db.yaml
   kubectl delete -f k8s/inventory-config.yaml
   ```
