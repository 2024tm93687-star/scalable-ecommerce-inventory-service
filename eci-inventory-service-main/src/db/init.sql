CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS inventory (
  inventory_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  product_id VARCHAR(64) NOT NULL,
  sku VARCHAR(64) NOT NULL,
  warehouse VARCHAR(64) NOT NULL,
  on_hand INT NOT NULL CHECK (on_hand >= 0),
  reserved INT NOT NULL DEFAULT 0 CHECK (reserved >= 0),
  updated_at TIMESTAMP NOT NULL DEFAULT NOW(),
  UNIQUE(product_id, warehouse)
);

CREATE TABLE IF NOT EXISTS inventory_movements (
  movement_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  product_id VARCHAR(64) NOT NULL,
  sku VARCHAR(64) NOT NULL,
  warehouse VARCHAR(64) NOT NULL,
  order_id VARCHAR(64) NOT NULL,
  type VARCHAR(20) NOT NULL CHECK (type IN ('RESERVE', 'RELEASE', 'SHIP')),
  quantity INT NOT NULL CHECK (quantity > 0),
  created_at TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS reservations (
  reservation_line_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  reservation_id UUID NOT NULL,
  order_id VARCHAR(64) NOT NULL,
  product_id VARCHAR(64) NOT NULL,
  sku VARCHAR(64) NOT NULL,
  warehouse VARCHAR(64) NOT NULL,
  quantity INT NOT NULL CHECK (quantity > 0),
  status VARCHAR(20) NOT NULL DEFAULT 'ACTIVE',
  expires_at TIMESTAMP NOT NULL,
  created_at TIMESTAMP NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS reserve_idempotency (
  idempotency_key VARCHAR(120) PRIMARY KEY,
  reservation_id UUID NOT NULL,
  response_json JSONB NOT NULL,
  created_at TIMESTAMP NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_inventory_sku ON inventory(sku);
CREATE INDEX IF NOT EXISTS idx_inventory_warehouse ON inventory(warehouse);
CREATE INDEX IF NOT EXISTS idx_movements_order_id ON inventory_movements(order_id);
CREATE INDEX IF NOT EXISTS idx_movements_product_id ON inventory_movements(product_id);
CREATE INDEX IF NOT EXISTS idx_reservations_order_id ON reservations(order_id);
CREATE INDEX IF NOT EXISTS idx_reservations_expires_at ON reservations(expires_at);
