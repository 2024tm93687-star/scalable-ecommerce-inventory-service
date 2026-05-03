const pino = require('pino');
const pinoHttp = require('pino-http');

const logger = pino({
  level: process.env.LOG_LEVEL || 'info',
  redact: {
    paths: [
      'req.headers.authorization',
      'req.body.email',
      'req.body.phone',
      'req.body.address',
      'req.body.customer.email',
      'req.body.customer.phone',
      'req.body.customer.address'
    ],
    remove: true
  }
});

const httpLogger = pinoHttp({ logger });

module.exports = { logger, httpLogger };
