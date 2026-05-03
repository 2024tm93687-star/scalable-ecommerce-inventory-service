require('dotenv').config();

const fs = require('fs');
const path = require('path');
const { parse } = require('csv-parse/sync');
const { pool } = require('../src/db/pool');

function datasetPath() {
  return process.env.ECI_DATASET_DIR || path.resolve(__dirname, '..', 'data');
}

function loadCsv(fileName) {
  const filePath = path.join(datasetPath(), fileName);
  if (!fs.existsSync(filePath)) {
    throw new Error(`Dataset file not found: ${filePath}`);
  }

  const content = fs.readFileSync(filePath, 'utf8');
  return parse(content, {
    columns: true,
    skip_empty_lines: true,
    trim: true,
    relax_column_count: true
  });
}

async function seedInventory() {
  const rows = loadCsv('eci_inventory_indian.csv');

  for (const row of rows) {
    const productId = String(row.product_id);
    const sku = String(row.sku || `SKU${row.product_id}`);
    const warehouse = String(row.warehouse || 'WH-1');
    const onHand = Number(row.on_hand || 0);
    const reserved = Number(row.reserved || 0);

    await pool.query(
      `INSERT INTO inventory (product_id, sku, warehouse, on_hand, reserved, updated_at)
       VALUES ($1, $2, $3, $4, $5, NOW())
       ON CONFLICT (product_id, warehouse) DO UPDATE
       SET sku = EXCLUDED.sku,
           on_hand = EXCLUDED.on_hand,
           reserved = EXCLUDED.reserved,
           updated_at = NOW()`,
      [productId, sku, warehouse, onHand, reserved]
    );
  }

  console.log(`Seeded inventory rows processed: ${rows.length}`);
}

async function run() {
  try {
    await seedInventory();
  } finally {
    await pool.end();
  }
}

run().catch((error) => {
  console.error('Inventory seed failed:', error.message);
  process.exit(1);
});
