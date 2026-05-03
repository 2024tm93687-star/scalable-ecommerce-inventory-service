const { randomUUID } = require('crypto');
const express = require('express');
const { pool } = require('../db/pool');
const { inventoryReserveLatencyMs, stockoutsTotal } = require('../metrics');

const router = express.Router();

function parsePagination(query) {
  const page = Math.max(parseInt(query.page || '1', 10), 1);
  const limit = Math.min(Math.max(parseInt(query.limit || '10', 10), 1), 100);
  const offset = (page - 1) * limit;
  return { page, limit, offset };
}

function roundToInt(value) {
  return Math.trunc(Number(value));
}

async function releaseExpiredReservations() {
  const client = await pool.connect();
  try {
    await client.query('BEGIN');
    const expired = await client.query(
      `SELECT * FROM reservations
       WHERE status = 'ACTIVE' AND expires_at <= NOW()`
    );

    for (const line of expired.rows) {
      await client.query(
        `UPDATE inventory
         SET reserved = reserved - $1,
             updated_at = NOW()
         WHERE product_id = $2 AND warehouse = $3`,
        [line.quantity, line.product_id, line.warehouse]
      );
      await client.query(
        `UPDATE reservations
         SET status = 'EXPIRED', updated_at = NOW()
         WHERE reservation_line_id = $1`,
        [line.reservation_line_id]
      );
      await client.query(
        `INSERT INTO inventory_movements (product_id, sku, warehouse, order_id, type, quantity)
         VALUES ($1, $2, $3, $4, 'RELEASE', $5)`,
        [line.product_id, line.sku, line.warehouse, line.order_id, line.quantity]
      );
    }

    await client.query('COMMIT');
  } catch (error) {
    await client.query('ROLLBACK');
    throw error;
  } finally {
    client.release();
  }
}

router.get('/', async (req, res, next) => {
  const { page, limit, offset } = parsePagination(req.query);
  const params = [];
  const filters = [];

  if (req.query.product_id) {
    params.push(req.query.product_id);
    filters.push(`product_id = $${params.length}`);
  }
  if (req.query.sku) {
    params.push(req.query.sku);
    filters.push(`sku = $${params.length}`);
  }
  if (req.query.warehouse) {
    params.push(req.query.warehouse);
    filters.push(`warehouse = $${params.length}`);
  }
  if (req.query.below_threshold === 'true') {
    const threshold = Number(process.env.LOW_STOCK_THRESHOLD || 5);
    params.push(threshold);
    filters.push(`(on_hand - reserved) < $${params.length}`);
  }

  const whereClause = filters.length ? `WHERE ${filters.join(' AND ')}` : '';

  try {
    const countQuery = `SELECT COUNT(*)::INT AS total FROM inventory ${whereClause}`;
    const dataQuery = `
      SELECT inventory_id, product_id, sku, warehouse, on_hand, reserved,
             (on_hand - reserved) AS available, updated_at
      FROM inventory
      ${whereClause}
      ORDER BY updated_at DESC
      LIMIT $${params.length + 1} OFFSET $${params.length + 2}
    `;

    const totalResult = await pool.query(countQuery, params);
    const dataResult = await pool.query(dataQuery, [...params, limit, offset]);
    return res.json({
      page,
      limit,
      total: totalResult.rows[0].total,
      items: dataResult.rows
    });
  } catch (error) {
    return next(error);
  }
});

router.post('/reserve', async (req, res, next) => {
  const endTimer = inventoryReserveLatencyMs.startTimer();
  const idempotencyKey = req.header('Idempotency-Key');
  if (!idempotencyKey) {
    return next({ status: 400, code: 'VALIDATION_ERROR', message: 'Idempotency-Key header is required' });
  }

  const { order_id, reservation_ttl_seconds, items } = req.body;
  if (!order_id || !Array.isArray(items) || items.length === 0) {
    return next({ status: 400, code: 'VALIDATION_ERROR', message: 'order_id and items are required' });
  }

  const client = await pool.connect();
  try {
    await client.query('BEGIN');
    const idem = await client.query('SELECT response_json FROM reserve_idempotency WHERE idempotency_key = $1', [idempotencyKey]);
    if (idem.rows.length) {
      await client.query('COMMIT');
      return res.status(200).json(idem.rows[0].response_json);
    }

    const reservationId = randomUUID();
    const ttl = Number(reservation_ttl_seconds || 900);
    const expiresAtResult = await client.query('SELECT NOW() + ($1::text || \' seconds\')::interval AS expires_at', [ttl]);
    const expiresAt = expiresAtResult.rows[0].expires_at;
    const allocations = [];

    for (const item of items) {
      if (!item.sku || !item.quantity || Number(item.quantity) <= 0) {
        return next({ status: 400, code: 'VALIDATION_ERROR', message: 'Each item requires sku and quantity > 0' });
      }

      const needed = roundToInt(item.quantity);
      const stocks = await client.query(
        `SELECT inventory_id, product_id, sku, warehouse, on_hand, reserved, (on_hand - reserved) AS available
         FROM inventory
         WHERE sku = $1
         ORDER BY (on_hand - reserved) DESC`,
        [item.sku]
      );

      if (!stocks.rows.length) {
        stockoutsTotal.inc();
        await client.query('ROLLBACK');
        return res.json({
          order_id,
          reservation_id: reservationId,
          status: 'REJECTED',
          expires_at: expiresAt,
          allocations: []
        });
      }

      const single = stocks.rows.find((row) => roundToInt(row.available) >= needed);
      const itemAllocations = [];

      if (single) {
        itemAllocations.push({
          product_id: single.product_id,
          sku: single.sku,
          warehouse: single.warehouse,
          quantity: needed
        });
      } else {
        let pending = needed;
        for (const row of stocks.rows) {
          const available = roundToInt(row.available);
          if (available <= 0) {
            continue;
          }
          const allocated = Math.min(available, pending);
          itemAllocations.push({
            product_id: row.product_id,
            sku: row.sku,
            warehouse: row.warehouse,
            quantity: allocated
          });
          pending -= allocated;
          if (pending === 0) {
            break;
          }
        }

        if (pending > 0) {
          stockoutsTotal.inc();
          await client.query('ROLLBACK');
          return res.json({
            order_id,
            reservation_id: reservationId,
            status: 'REJECTED',
            expires_at: expiresAt,
            allocations: []
          });
        }
      }

      for (const allocation of itemAllocations) {
        const rowLock = await client.query(
          `SELECT on_hand, reserved FROM inventory
           WHERE product_id = $1 AND warehouse = $2
           FOR UPDATE`,
          [allocation.product_id, allocation.warehouse]
        );
        const availableNow = roundToInt(rowLock.rows[0].on_hand) - roundToInt(rowLock.rows[0].reserved);
        if (availableNow < allocation.quantity) {
          stockoutsTotal.inc();
          await client.query('ROLLBACK');
          return res.json({
            order_id,
            reservation_id: reservationId,
            status: 'REJECTED',
            expires_at: expiresAt,
            allocations: []
          });
        }

        await client.query(
          `UPDATE inventory
           SET reserved = reserved + $1,
               updated_at = NOW()
           WHERE product_id = $2 AND warehouse = $3`,
          [allocation.quantity, allocation.product_id, allocation.warehouse]
        );

        await client.query(
          `INSERT INTO reservations (reservation_id, order_id, product_id, sku, warehouse, quantity, status, expires_at)
           VALUES ($1, $2, $3, $4, $5, $6, 'ACTIVE', $7)`,
          [reservationId, order_id, allocation.product_id, allocation.sku, allocation.warehouse, allocation.quantity, expiresAt]
        );

        await client.query(
          `INSERT INTO inventory_movements (product_id, sku, warehouse, order_id, type, quantity)
           VALUES ($1, $2, $3, $4, 'RESERVE', $5)`,
          [allocation.product_id, allocation.sku, allocation.warehouse, order_id, allocation.quantity]
        );

        allocations.push({
          sku: allocation.sku,
          product_id: allocation.product_id,
          warehouse: allocation.warehouse,
          quantity: allocation.quantity
        });
      }
    }

    const responsePayload = {
      order_id,
      reservation_id: reservationId,
      status: 'FULLY_RESERVED',
      expires_at: expiresAt,
      allocations
    };

    await client.query(
      `INSERT INTO reserve_idempotency (idempotency_key, reservation_id, response_json)
       VALUES ($1, $2, $3::jsonb)`,
      [idempotencyKey, reservationId, JSON.stringify(responsePayload)]
    );

    await client.query('COMMIT');
    return res.status(200).json(responsePayload);
  } catch (error) {
    await client.query('ROLLBACK');
    return next(error);
  } finally {
    client.release();
    endTimer();
  }
});

router.post('/release', async (req, res, next) => {
  const { order_id } = req.body;
  if (!order_id) {
    return next({ status: 400, code: 'VALIDATION_ERROR', message: 'order_id is required' });
  }

  const client = await pool.connect();
  try {
    await client.query('BEGIN');
    const lines = await client.query(
      `SELECT * FROM reservations
       WHERE order_id = $1 AND status = 'ACTIVE'`,
      [order_id]
    );

    if (!lines.rows.length) {
      await client.query('COMMIT');
      return next({ status: 404, code: 'RESERVATION_NOT_FOUND', message: 'No active reservation found for order' });
    }

    for (const line of lines.rows) {
      await client.query(
        `UPDATE inventory
         SET reserved = reserved - $1, updated_at = NOW()
         WHERE product_id = $2 AND warehouse = $3`,
        [line.quantity, line.product_id, line.warehouse]
      );
      await client.query(
        `UPDATE reservations
         SET status = 'RELEASED', updated_at = NOW()
         WHERE reservation_line_id = $1`,
        [line.reservation_line_id]
      );
      await client.query(
        `INSERT INTO inventory_movements (product_id, sku, warehouse, order_id, type, quantity)
         VALUES ($1, $2, $3, $4, 'RELEASE', $5)`,
        [line.product_id, line.sku, line.warehouse, order_id, line.quantity]
      );
    }

    await client.query('COMMIT');
    return res.json({ order_id, released: lines.rows.length });
  } catch (error) {
    await client.query('ROLLBACK');
    return next(error);
  } finally {
    client.release();
  }
});

router.post('/ship', async (req, res, next) => {
  const { order_id, shipment_id } = req.body;
  if (!order_id || !shipment_id) {
    return next({ status: 400, code: 'VALIDATION_ERROR', message: 'order_id and shipment_id are required' });
  }

  const client = await pool.connect();
  try {
    await client.query('BEGIN');
    const lines = await client.query(
      `SELECT * FROM reservations
       WHERE order_id = $1 AND status = 'ACTIVE'`,
      [order_id]
    );

    if (!lines.rows.length) {
      await client.query('COMMIT');
      return next({ status: 404, code: 'RESERVATION_NOT_FOUND', message: 'No active reservation found for order' });
    }

    for (const line of lines.rows) {
      await client.query(
        `UPDATE inventory
         SET on_hand = on_hand - $1,
             reserved = reserved - $1,
             updated_at = NOW()
         WHERE product_id = $2 AND warehouse = $3`,
        [line.quantity, line.product_id, line.warehouse]
      );
      await client.query(
        `UPDATE reservations
         SET status = 'SHIPPED', updated_at = NOW()
         WHERE reservation_line_id = $1`,
        [line.reservation_line_id]
      );
      await client.query(
        `INSERT INTO inventory_movements (product_id, sku, warehouse, order_id, type, quantity)
         VALUES ($1, $2, $3, $4, 'SHIP', $5)`,
        [line.product_id, line.sku, line.warehouse, order_id, line.quantity]
      );

      const lowStockCheck = await client.query(
        'SELECT (on_hand - reserved) AS available FROM inventory WHERE product_id = $1 AND warehouse = $2',
        [line.product_id, line.warehouse]
      );
      if (roundToInt(lowStockCheck.rows[0].available) < Number(process.env.LOW_STOCK_THRESHOLD || 5)) {
        stockoutsTotal.inc();
      }
    }

    await client.query('COMMIT');
    return res.json({ order_id, shipment_id, shipped: true });
  } catch (error) {
    await client.query('ROLLBACK');
    return next(error);
  } finally {
    client.release();
  }
});

router.get('/movements', async (req, res, next) => {
  const { page, limit, offset } = parsePagination(req.query);
  const params = [];
  const filters = [];

  if (req.query.order_id) {
    params.push(req.query.order_id);
    filters.push(`order_id = $${params.length}`);
  }
  if (req.query.product_id) {
    params.push(req.query.product_id);
    filters.push(`product_id = $${params.length}`);
  }
  if (req.query.warehouse) {
    params.push(req.query.warehouse);
    filters.push(`warehouse = $${params.length}`);
  }
  if (req.query.type) {
    params.push(req.query.type);
    filters.push(`type = $${params.length}`);
  }

  const whereClause = filters.length ? `WHERE ${filters.join(' AND ')}` : '';

  try {
    const countQuery = `SELECT COUNT(*)::INT AS total FROM inventory_movements ${whereClause}`;
    const dataQuery = `
      SELECT movement_id, product_id, warehouse, order_id, type, quantity, created_at
      FROM inventory_movements
      ${whereClause}
      ORDER BY created_at DESC
      LIMIT $${params.length + 1} OFFSET $${params.length + 2}
    `;
    const totalResult = await pool.query(countQuery, params);
    const dataResult = await pool.query(dataQuery, [...params, limit, offset]);

    return res.json({
      page,
      limit,
      total: totalResult.rows[0].total,
      items: dataResult.rows
    });
  } catch (error) {
    return next(error);
  }
});

module.exports = {
  router,
  releaseExpiredReservations
};