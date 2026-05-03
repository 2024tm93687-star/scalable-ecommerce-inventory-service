const fs = require('fs');
const path = require('path');
const app = require('./app');
const { pool } = require('./db/pool');

const port = process.env.PORT || 3002;

async function bootstrap() {
  const initSqlPath = path.join(__dirname, 'db', 'init.sql');
  const initSql = fs.readFileSync(initSqlPath, 'utf8');
  await pool.query(initSql);

  app.listen(port, () => {
    console.log(`inventory-service listening on port ${port}`);
  });
}

bootstrap().catch((err) => {
  console.error('Failed to bootstrap inventory-service', err);
  process.exit(1);
});
