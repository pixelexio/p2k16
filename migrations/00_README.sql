/*
Template migration.
*/

DROP TABLE IF EXISTS foo_version;
DROP TABLE IF EXISTS foo;

CREATE TABLE foo (
  id         BIGSERIAL                NOT NULL PRIMARY KEY,

  created_at TIMESTAMP WITH TIME ZONE NOT NULL,
  created_by BIGINT                   NOT NULL REFERENCES account,
  updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
  updated_by BIGINT                   NOT NULL REFERENCES account,

  -- Insert the model's fields here
);
GRANT ALL ON foo TO "p2k16-web";
GRANT ALL ON foo_id_seq TO "p2k16-web";

CREATE TABLE account_badge_version
(
  transaction_id     BIGINT                   NOT NULL REFERENCES transaction,
  end_transaction_id BIGINT REFERENCES transaction,
  operation_type     INT                      NOT NULL,

  id                 BIGSERIAL                NOT NULL,

  created_at         TIMESTAMP WITH TIME ZONE NOT NULL,
  created_by         BIGINT                   NOT NULL,
  updated_at         TIMESTAMP WITH TIME ZONE NOT NULL,
  updated_by         BIGINT                   NOT NULL,

  -- Insert the model's fields here
);
GRANT INSERT, UPDATE ON foo_version TO "p2k16-web";
GRANT ALL ON foo_version TO "p2k16-web";
