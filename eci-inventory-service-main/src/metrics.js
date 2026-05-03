const client = require('prom-client');

client.collectDefaultMetrics();

const inventoryReserveLatencyMs = new client.Histogram({
  name: 'inventory_reserve_latency_ms',
  help: 'Latency of inventory reservations in milliseconds',
  buckets: [5, 10, 25, 50, 100, 250, 500, 1000]
});

const stockoutsTotal = new client.Counter({
  name: 'stockouts_total',
  help: 'Number of stockout events during reserve attempts'
});

module.exports = {
  register: client.register,
  inventoryReserveLatencyMs,
  stockoutsTotal
};
