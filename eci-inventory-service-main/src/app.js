require('dotenv').config();

const express = require('express');
const helmet = require('helmet');
const swaggerUi = require('swagger-ui-express');
const YAML = require('yamljs');
const { pool } = require('./db/pool');
const { register } = require('./metrics');
const { contextMiddleware, errorResponse } = require('./middleware/context');
const { httpLogger } = require('./middleware/logger');
const inventoryRoutes = require('./routes/inventory');

const openApiDocument = YAML.load('./openapi/inventory.openapi.yaml');

const app = express();
app.use(helmet());
app.use(express.json());
app.use(contextMiddleware);
app.use(httpLogger);

app.get('/health', async (req, res) => {
  await pool.query('SELECT 1');
  res.json({ status: 'ok', service: process.env.SERVICE_NAME || 'inventory-service' });
});

app.get('/metrics', async (req, res) => {
  res.set('Content-Type', register.contentType);
  res.end(await register.metrics());
});

app.use('/docs', swaggerUi.serve, swaggerUi.setup(openApiDocument));
app.use('/v1/inventory', inventoryRoutes.router);

setInterval(() => {
  inventoryRoutes.releaseExpiredReservations().catch(() => {});
}, 60 * 1000);

app.use((req, res) => {
  res.status(404).json({
    code: 'NOT_FOUND',
    message: 'Route does not exist',
    correlationId: req.correlationId
  });
});

app.use(errorResponse);

module.exports = app;
