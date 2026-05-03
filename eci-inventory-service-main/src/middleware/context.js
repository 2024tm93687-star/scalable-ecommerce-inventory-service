const { randomUUID } = require('crypto');

function contextMiddleware(req, res, next) {
  const correlationId = req.header('x-correlation-id') || randomUUID();
  req.correlationId = correlationId;
  res.setHeader('x-correlation-id', correlationId);
  next();
}

function errorResponse(err, req, res, next) {
  const status = err.status || 500;
  res.status(status).json({
    code: err.code || 'INTERNAL_ERROR',
    message: err.message || 'Unexpected error occurred',
    correlationId: req.correlationId
  });
}

module.exports = { contextMiddleware, errorResponse };
