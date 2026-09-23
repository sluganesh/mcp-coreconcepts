CREATE TABLE employees (
    id          SERIAL PRIMARY KEY,
    first_name  VARCHAR(50)    NOT NULL,
    last_name   VARCHAR(50)    NOT NULL,
    email       VARCHAR(100)   NOT NULL UNIQUE,
    department  VARCHAR(50)    NOT NULL,
    job_title   VARCHAR(100)   NOT NULL,
    salary      NUMERIC(10, 2) NOT NULL,
    hire_date   DATE           NOT NULL
);

INSERT INTO employees (first_name, last_name, email, department, job_title, salary, hire_date) VALUES
    ('Aarav',  'Sharma',   'aarav.sharma@example.com',   'Engineering', 'Senior Software Engineer', 125000.00, '2019-03-15'),
    ('Priya',  'Iyer',     'priya.iyer@example.com',     'Engineering', 'Software Engineer',         98000.00, '2021-07-01'),
    ('Rahul',  'Verma',    'rahul.verma@example.com',    'Engineering', 'Engineering Manager',      145000.00, '2017-11-20'),
    ('Sneha',  'Reddy',    'sneha.reddy@example.com',    'Marketing',   'Marketing Specialist',      72000.00, '2022-01-10'),
    ('Vikram', 'Nair',     'vikram.nair@example.com',    'Marketing',   'Marketing Manager',         95000.00, '2018-05-22'),
    ('Ananya', 'Gupta',    'ananya.gupta@example.com',   'Sales',       'Account Executive',         68000.00, '2023-02-14'),
    ('Karthik','Menon',    'karthik.menon@example.com',  'Sales',       'Sales Director',           130000.00, '2016-09-05'),
    ('Divya',  'Pillai',   'divya.pillai@example.com',   'HR',          'HR Generalist',             64000.00, '2020-04-18'),
    ('Arjun',  'Kapoor',   'arjun.kapoor@example.com',   'Finance',     'Financial Analyst',         82000.00, '2021-10-25'),
    ('Meera',  'Joshi',    'meera.joshi@example.com',    'Finance',     'Finance Manager',          115000.00, '2015-06-30');
