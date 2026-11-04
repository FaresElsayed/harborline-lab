-- Microsoft Dynamics AX 2012 R3 (database: ax2012, schema: dbo)
-- Replica of the AX table layout that Harborline Finance runs on-prem.
-- Conventions copied from AX:
--   * every table has RECID (bigint surrogate key, target of every relation), RECVERSION, PARTITION
--   * company-specific tables carry DATAAREAID ('us01', 'ca01', 'mx01'); shared tables do not
--   * CREATEDDATETIME / MODIFIEDDATETIME are UTC (stored without time zone)
--   * enums are stored as integers (lookup: srsanalysisenums); NoYes: 0 = No, 1 = Yes
--   * an "empty" date is 1900-01-01; an open-ended VALIDTO is 2154-12-31 23:59:59
--   * there are NO foreign keys in the database - relations exist only in the application
-- Table and column names are lower case here (AX uses upper case on SQL Server).

CREATE SCHEMA IF NOT EXISTS dbo;
SET search_path = dbo;

CREATE TABLE dataarea (
  id varchar(4) PRIMARY KEY, name varchar(60) NOT NULL, isvirtual int NOT NULL DEFAULT 0,
  recid bigint NOT NULL UNIQUE, recversion int NOT NULL DEFAULT 1, partition bigint NOT NULL DEFAULT 5637144576
);

CREATE TABLE dirpartytable (
  recid bigint PRIMARY KEY, partynumber varchar(20) NOT NULL, name varchar(100) NOT NULL,
  namealias varchar(20), knownas varchar(100), languageid varchar(7),
  instancerelationtype int NOT NULL,               -- table id of subtype: 2978 = DirOrganization, 2975 = DirPerson
  primaryaddresslocation bigint, primarycontactemail bigint, primarycontactphone bigint,
  modifieddatetime timestamp NOT NULL, modifiedby varchar(20), createddatetime timestamp NOT NULL, createdby varchar(20),
  recversion int NOT NULL DEFAULT 1, partition bigint NOT NULL DEFAULT 5637144576
);

CREATE TABLE logisticslocation (
  recid bigint PRIMARY KEY, locationid varchar(20) NOT NULL, description varchar(60),
  ispostaladdress int NOT NULL DEFAULT 1, parentlocation bigint NOT NULL DEFAULT 0,
  modifieddatetime timestamp NOT NULL, createddatetime timestamp NOT NULL,
  recversion int NOT NULL DEFAULT 1, partition bigint NOT NULL DEFAULT 5637144576
);

CREATE TABLE dirpartylocation (
  recid bigint PRIMARY KEY, party bigint NOT NULL, location bigint NOT NULL,
  isprimary int NOT NULL DEFAULT 0, ispostaladdress int NOT NULL DEFAULT 1, islocationowner int NOT NULL DEFAULT 1,
  isprivate int NOT NULL DEFAULT 0,
  modifieddatetime timestamp NOT NULL, createddatetime timestamp NOT NULL,
  recversion int NOT NULL DEFAULT 1, partition bigint NOT NULL DEFAULT 5637144576
);

CREATE TABLE logisticspostaladdress (
  recid bigint PRIMARY KEY, location bigint NOT NULL, address varchar(250),
  street varchar(250), streetnumber varchar(20), city varchar(60), county varchar(30), state varchar(10),
  zipcode varchar(10), countryregionid varchar(10), districtname varchar(60), postbox varchar(10),
  latitude numeric(32,16), longitude numeric(32,16), timezone int NOT NULL DEFAULT 0,
  validfrom timestamp NOT NULL, validfromtzid int NOT NULL DEFAULT 0,
  validto timestamp NOT NULL, validtotzid int NOT NULL DEFAULT 0,
  isprivate int NOT NULL DEFAULT 0, privateforparty bigint NOT NULL DEFAULT 0,
  modifieddatetime timestamp NOT NULL, createddatetime timestamp NOT NULL,
  recversion int NOT NULL DEFAULT 1, partition bigint NOT NULL DEFAULT 5637144576
);
COMMENT ON TABLE logisticspostaladdress IS 'Date-effective. An address change sets VALIDTO on the current row and inserts a new row.';

CREATE TABLE dirpersonname (
  recid bigint PRIMARY KEY, person bigint NOT NULL, firstname varchar(25), middlename varchar(25),
  lastnameprefix varchar(25), lastname varchar(25),
  validfrom timestamp NOT NULL, validto timestamp NOT NULL,
  modifieddatetime timestamp NOT NULL, createddatetime timestamp NOT NULL,
  recversion int NOT NULL DEFAULT 1, partition bigint NOT NULL DEFAULT 5637144576
);

CREATE TABLE hcmworker (
  recid bigint PRIMARY KEY, person bigint NOT NULL, personnelnumber varchar(25) NOT NULL,
  modifieddatetime timestamp NOT NULL, createddatetime timestamp NOT NULL,
  recversion int NOT NULL DEFAULT 1, partition bigint NOT NULL DEFAULT 5637144576
);

CREATE TABLE hcmemployment (
  recid bigint PRIMARY KEY, worker bigint NOT NULL, legalentity bigint NOT NULL,
  employmenttype int NOT NULL DEFAULT 0,           -- 0 Employee, 1 Contractor
  validfrom timestamp NOT NULL, validto timestamp NOT NULL,
  defaultdimension bigint NOT NULL DEFAULT 0,
  modifieddatetime timestamp NOT NULL, createddatetime timestamp NOT NULL,
  recversion int NOT NULL DEFAULT 1, partition bigint NOT NULL DEFAULT 5637144576
);

CREATE TABLE paymterm (
  recid bigint PRIMARY KEY, dataareaid varchar(4) NOT NULL, paymtermid varchar(10) NOT NULL,
  description varchar(60), paymmethod int NOT NULL DEFAULT 0,   -- 0 Net, 1 CurrentMonth, 2 CurrentQuarter...
  numofdays int NOT NULL DEFAULT 0, numofmonths int NOT NULL DEFAULT 0, cash varchar(10), cutoffday int NOT NULL DEFAULT 0,
  modifieddatetime timestamp NOT NULL, createddatetime timestamp NOT NULL,
  recversion int NOT NULL DEFAULT 1, partition bigint NOT NULL DEFAULT 5637144576
);

CREATE TABLE custtable (
  recid bigint PRIMARY KEY, dataareaid varchar(4) NOT NULL, accountnum varchar(20) NOT NULL,
  party bigint NOT NULL,                       -- dirpartytable.recid (the name lives there)
  custgroup varchar(10) NOT NULL, currency varchar(3) NOT NULL, paymtermid varchar(10), paymmode varchar(10),
  creditmax numeric(32,16) NOT NULL DEFAULT 0, invoiceaccount varchar(20), taxgroup varchar(10), vatnum varchar(20),
  blocked int NOT NULL DEFAULT 0,              -- CustVendorBlocked: 0 No, 1 Invoice, 2 All
  dlvmode varchar(10), inventlocation varchar(10), salesgroup varchar(10), lineofbusinessid varchar(10),
  segmentid varchar(20), defaultdimension bigint NOT NULL DEFAULT 0, onetimecustomer int NOT NULL DEFAULT 0,
  modifieddatetime timestamp NOT NULL, modifiedby varchar(20), createddatetime timestamp NOT NULL, createdby varchar(20),
  recversion int NOT NULL DEFAULT 1, partition bigint NOT NULL DEFAULT 5637144576
);
COMMENT ON TABLE custtable IS 'Customers per legal entity. A client served by US and CA sites has one row in us01 and one in ca01.';

CREATE TABLE vendtable (
  recid bigint PRIMARY KEY, dataareaid varchar(4) NOT NULL, accountnum varchar(20) NOT NULL, party bigint NOT NULL,
  vendgroup varchar(10) NOT NULL,              -- CARRIER, SUPPLIES, LANDLORD, UTILITY, IT
  currency varchar(3) NOT NULL, paymtermid varchar(10), paymmode varchar(10), cashdisc varchar(10),
  blocked int NOT NULL DEFAULT 0, invoiceaccount varchar(20), taxgroup varchar(10), vatnum varchar(20),
  tax1099reports int NOT NULL DEFAULT 0, onetimevendor int NOT NULL DEFAULT 0, defaultdimension bigint NOT NULL DEFAULT 0,
  modifieddatetime timestamp NOT NULL, modifiedby varchar(20), createddatetime timestamp NOT NULL, createdby varchar(20),
  recversion int NOT NULL DEFAULT 1, partition bigint NOT NULL DEFAULT 5637144576
);

CREATE TABLE currency (
  recid bigint PRIMARY KEY, currencycode varchar(3) NOT NULL, txt varchar(60), currencycodeiso varchar(3),
  symbol varchar(5), roundingprecision numeric(32,16) NOT NULL DEFAULT 0.01,
  modifieddatetime timestamp NOT NULL, createddatetime timestamp NOT NULL,
  recversion int NOT NULL DEFAULT 1, partition bigint NOT NULL DEFAULT 5637144576
);

CREATE TABLE exchangeratetype (
  recid bigint PRIMARY KEY, name varchar(20) NOT NULL, description varchar(60),
  modifieddatetime timestamp NOT NULL, createddatetime timestamp NOT NULL,
  recversion int NOT NULL DEFAULT 1, partition bigint NOT NULL DEFAULT 5637144576
);

CREATE TABLE exchangeratecurrencypair (
  recid bigint PRIMARY KEY, exchangeratetype bigint NOT NULL, fromcurrencycode varchar(3) NOT NULL,
  tocurrencycode varchar(3) NOT NULL, exchangeratedisplayfactor int NOT NULL DEFAULT 0,   -- 0 = per 1, 2 = per 100
  modifieddatetime timestamp NOT NULL, createddatetime timestamp NOT NULL,
  recversion int NOT NULL DEFAULT 1, partition bigint NOT NULL DEFAULT 5637144576
);

CREATE TABLE exchangerate (
  recid bigint PRIMARY KEY, exchangeratecurrencypair bigint NOT NULL,
  exchangerate numeric(32,16) NOT NULL,       -- stored x100: 135.2 means 1 EUR = 1.352 USD
  validfrom date NOT NULL, validto date NOT NULL,
  modifieddatetime timestamp NOT NULL, createddatetime timestamp NOT NULL,
  recversion int NOT NULL DEFAULT 1, partition bigint NOT NULL DEFAULT 5637144576
);
COMMENT ON TABLE exchangerate IS 'Month-end accounting rates keyed by Finance. Stored x100 per AX convention.';

CREATE TABLE mainaccount (
  recid bigint PRIMARY KEY, mainaccountid varchar(20) NOT NULL, name varchar(60) NOT NULL,
  type int NOT NULL,                           -- DimensionLedgerAccountType: 0 ProfitAndLoss, 1 Revenue, 2 Expense, 4 BalanceSheet, 5 Asset, 6 Liability, 7 Equity
  ledgerchartofaccounts bigint NOT NULL DEFAULT 0, currencycode varchar(3), debitcreditproposal int NOT NULL DEFAULT 0,
  monetary int NOT NULL DEFAULT 1, closetype int NOT NULL DEFAULT 0,
  modifieddatetime timestamp NOT NULL, createddatetime timestamp NOT NULL,
  recversion int NOT NULL DEFAULT 1, partition bigint NOT NULL DEFAULT 5637144576
);

CREATE TABLE dimensionattributevaluecombination (
  recid bigint PRIMARY KEY, displayvalue varchar(500) NOT NULL,   -- e.g. 4010-ATL1-OPS
  mainaccount bigint NOT NULL DEFAULT 0, accountstructure bigint NOT NULL DEFAULT 0,
  ledgerdimensiontype int NOT NULL DEFAULT 0, hash varchar(40),
  modifieddatetime timestamp NOT NULL, createddatetime timestamp NOT NULL,
  recversion int NOT NULL DEFAULT 1, partition bigint NOT NULL DEFAULT 5637144576
);

CREATE TABLE ledger (
  recid bigint PRIMARY KEY, name varchar(60) NOT NULL, primaryforlegalentity bigint NOT NULL,
  accountingcurrency varchar(3) NOT NULL, reportingcurrency varchar(3),
  modifieddatetime timestamp NOT NULL, createddatetime timestamp NOT NULL,
  recversion int NOT NULL DEFAULT 1, partition bigint NOT NULL DEFAULT 5637144576
);

CREATE TABLE custinvoicejour (
  recid bigint PRIMARY KEY, dataareaid varchar(4) NOT NULL, invoiceid varchar(20) NOT NULL, invoicedate date NOT NULL,
  invoiceaccount varchar(20) NOT NULL, orderaccount varchar(20) NOT NULL, salesid varchar(20),
  numbersequencegroup varchar(10), currencycode varchar(3) NOT NULL, exchrate numeric(32,16) NOT NULL,
  invoiceamount numeric(32,16) NOT NULL, invoiceamountmst numeric(32,16) NOT NULL, salesbalance numeric(32,16) NOT NULL,
  sumtax numeric(32,16) NOT NULL DEFAULT 0, sumlinedisc numeric(32,16) NOT NULL DEFAULT 0, summarkup numeric(32,16) NOT NULL DEFAULT 0,
  duedate date NOT NULL, ledgervoucher varchar(20), payment varchar(10), dlvmode varchar(10),
  qty numeric(32,16) NOT NULL DEFAULT 0, weight numeric(32,16) NOT NULL DEFAULT 0, custgroup varchar(10),
  defaultdimension bigint NOT NULL DEFAULT 0,
  modifieddatetime timestamp NOT NULL, modifiedby varchar(20), createddatetime timestamp NOT NULL, createdby varchar(20),
  recversion int NOT NULL DEFAULT 1, partition bigint NOT NULL DEFAULT 5637144576
);
COMMENT ON TABLE custinvoicejour IS 'Posted customer invoices (immutable). Credit notes are invoices with negative amounts.';

CREATE TABLE custinvoicetrans (
  recid bigint PRIMARY KEY, dataareaid varchar(4) NOT NULL, invoiceid varchar(20) NOT NULL, invoicedate date NOT NULL,
  salesid varchar(20), numbersequencegroup varchar(10), linenum numeric(32,16) NOT NULL,
  itemid varchar(20),                           -- service item = charge code; blank on free-text lines
  name varchar(1000), qty numeric(32,16) NOT NULL, salesunit varchar(10), salesprice numeric(32,16) NOT NULL,
  priceunit numeric(32,16) NOT NULL DEFAULT 1, lineamount numeric(32,16) NOT NULL, lineamountmst numeric(32,16) NOT NULL,
  taxamount numeric(32,16) NOT NULL DEFAULT 0, discamount numeric(32,16) NOT NULL DEFAULT 0, currencycode varchar(3) NOT NULL,
  inventdimid varchar(20), inventtransid varchar(20), dlvdate date, ledgerdimension bigint NOT NULL DEFAULT 0,
  defaultdimension bigint NOT NULL DEFAULT 0,
  modifieddatetime timestamp NOT NULL, createddatetime timestamp NOT NULL,
  recversion int NOT NULL DEFAULT 1, partition bigint NOT NULL DEFAULT 5637144576
);

CREATE TABLE custtrans (
  recid bigint PRIMARY KEY, dataareaid varchar(4) NOT NULL, accountnum varchar(20) NOT NULL, voucher varchar(20) NOT NULL,
  transdate date NOT NULL, transtype int NOT NULL,      -- LedgerTransType: 2 Sales, 8 Cust, 15 Payment, 24 Settlement, 35 WriteOff, 36 GeneralJournal
  invoice varchar(20), txt varchar(60),
  amountcur numeric(32,16) NOT NULL, amountmst numeric(32,16) NOT NULL,
  settleamountcur numeric(32,16) NOT NULL DEFAULT 0, settleamountmst numeric(32,16) NOT NULL DEFAULT 0,
  currencycode varchar(3) NOT NULL, exchrate numeric(32,16) NOT NULL, duedate date NOT NULL,
  closed date NOT NULL DEFAULT '1900-01-01', lastsettlevoucher varchar(20), lastsettledate date NOT NULL DEFAULT '1900-01-01',
  paymmode varchar(10), paymreference varchar(30), postingprofile varchar(10), documentnum varchar(20), documentdate date,
  orderaccount varchar(20), approved int NOT NULL DEFAULT 1, correct int NOT NULL DEFAULT 0, prepayment int NOT NULL DEFAULT 0,
  offsetrecid bigint NOT NULL DEFAULT 0, defaultdimension bigint NOT NULL DEFAULT 0, reportingcurrencyamount numeric(32,16) NOT NULL DEFAULT 0,
  modifieddatetime timestamp NOT NULL, modifiedby varchar(20), createddatetime timestamp NOT NULL, createdby varchar(20),
  recversion int NOT NULL DEFAULT 1, partition bigint NOT NULL DEFAULT 5637144576
);
COMMENT ON TABLE custtrans IS 'Customer ledger. Invoice rows are UPDATED when settled (settleamountcur, closed, lastsettledate).';

CREATE TABLE custsettlement (
  recid bigint PRIMARY KEY, dataareaid varchar(4) NOT NULL, transrecid bigint NOT NULL, transcompany varchar(4),
  transdate date NOT NULL, accountnum varchar(20) NOT NULL, offsetrecid bigint NOT NULL, offsettransvoucher varchar(20),
  offsetaccountnum varchar(20), offsetcompany varchar(4), settleamountcur numeric(32,16) NOT NULL, settleamountmst numeric(32,16) NOT NULL,
  exchadjustment numeric(32,16) NOT NULL DEFAULT 0, utilizedcashdisc numeric(32,16) NOT NULL DEFAULT 0, duedate date,
  settlementgroup bigint NOT NULL DEFAULT 0, canbereversed int NOT NULL DEFAULT 1,
  modifieddatetime timestamp NOT NULL, createddatetime timestamp NOT NULL,
  recversion int NOT NULL DEFAULT 1, partition bigint NOT NULL DEFAULT 5637144576
);
COMMENT ON TABLE custsettlement IS 'Links a payment to the invoice it settles. Reversed settlements are DELETED.';

CREATE TABLE vendinvoicejour (
  recid bigint PRIMARY KEY, dataareaid varchar(4) NOT NULL, invoiceid varchar(30) NOT NULL, internalinvoiceid varchar(20) NOT NULL,
  invoicedate date NOT NULL, documentdate date, invoiceaccount varchar(20) NOT NULL, orderaccount varchar(20) NOT NULL,
  purchid varchar(20), currencycode varchar(3) NOT NULL, exchrate numeric(32,16) NOT NULL,
  invoiceamount numeric(32,16) NOT NULL, invoiceamountmst numeric(32,16) NOT NULL, sumtax numeric(32,16) NOT NULL DEFAULT 0,
  duedate date NOT NULL, ledgervoucher varchar(20), payment varchar(10), vendgroup varchar(10), numbersequencegroup varchar(10),
  modifieddatetime timestamp NOT NULL, modifiedby varchar(20), createddatetime timestamp NOT NULL, createdby varchar(20),
  recversion int NOT NULL DEFAULT 1, partition bigint NOT NULL DEFAULT 5637144576
);
COMMENT ON TABLE vendinvoicejour IS 'Posted vendor invoices. Carrier freight bills arrive weekly per carrier.';

CREATE TABLE vendinvoicetrans (
  recid bigint PRIMARY KEY, dataareaid varchar(4) NOT NULL, invoiceid varchar(30) NOT NULL, internalinvoiceid varchar(20) NOT NULL,
  invoicedate date NOT NULL, linenum numeric(32,16) NOT NULL, itemid varchar(20),
  name varchar(1000),                          -- freight bills: free text such as "TRK 1Z... WT 12.4LB ZN 5 GND"
  qty numeric(32,16) NOT NULL, purchprice numeric(32,16) NOT NULL, lineamount numeric(32,16) NOT NULL,
  lineamountmst numeric(32,16) NOT NULL, currencycode varchar(3) NOT NULL, ledgerdimension bigint NOT NULL DEFAULT 0,
  modifieddatetime timestamp NOT NULL, createddatetime timestamp NOT NULL,
  recversion int NOT NULL DEFAULT 1, partition bigint NOT NULL DEFAULT 5637144576
);

CREATE TABLE vendtrans (
  recid bigint PRIMARY KEY, dataareaid varchar(4) NOT NULL, accountnum varchar(20) NOT NULL, voucher varchar(20) NOT NULL,
  transdate date NOT NULL, transtype int NOT NULL, invoice varchar(30), txt varchar(60),
  amountcur numeric(32,16) NOT NULL, amountmst numeric(32,16) NOT NULL,
  settleamountcur numeric(32,16) NOT NULL DEFAULT 0, settleamountmst numeric(32,16) NOT NULL DEFAULT 0,
  currencycode varchar(3) NOT NULL, exchrate numeric(32,16) NOT NULL, duedate date NOT NULL,
  closed date NOT NULL DEFAULT '1900-01-01', lastsettledate date NOT NULL DEFAULT '1900-01-01',
  approved int NOT NULL DEFAULT 1, approveddate date, journalnum varchar(20), paymtermid varchar(10),
  modifieddatetime timestamp NOT NULL, modifiedby varchar(20), createddatetime timestamp NOT NULL, createdby varchar(20),
  recversion int NOT NULL DEFAULT 1, partition bigint NOT NULL DEFAULT 5637144576
);

CREATE TABLE ledgerjournaltable (
  recid bigint PRIMARY KEY, dataareaid varchar(4) NOT NULL, journalnum varchar(20) NOT NULL, journalname varchar(10) NOT NULL,
  name varchar(60), journaltype int NOT NULL DEFAULT 0,   -- 0 Daily, 3 VendInvoiceRegister, 9 CustPayment
  posted int NOT NULL DEFAULT 0, posteddatetime timestamp,
  modifieddatetime timestamp NOT NULL, modifiedby varchar(20), createddatetime timestamp NOT NULL, createdby varchar(20),
  recversion int NOT NULL DEFAULT 1, partition bigint NOT NULL DEFAULT 5637144576
);

CREATE TABLE ledgerjournaltrans (
  recid bigint PRIMARY KEY, dataareaid varchar(4) NOT NULL, journalnum varchar(20) NOT NULL, linenum numeric(32,16) NOT NULL,
  transdate date NOT NULL, accounttype int NOT NULL DEFAULT 0,  -- 0 Ledger, 1 Cust, 2 Vend, 6 Bank
  ledgerdimension bigint NOT NULL DEFAULT 0, txt varchar(60),
  amountcurdebit numeric(32,16) NOT NULL DEFAULT 0, amountcurcredit numeric(32,16) NOT NULL DEFAULT 0,
  currencycode varchar(3) NOT NULL, exchrate numeric(32,16) NOT NULL DEFAULT 100, voucher varchar(20),
  modifieddatetime timestamp NOT NULL, modifiedby varchar(20), createddatetime timestamp NOT NULL, createdby varchar(20),
  recversion int NOT NULL DEFAULT 1, partition bigint NOT NULL DEFAULT 5637144576
);
COMMENT ON TABLE ledgerjournaltrans IS 'Journal lines. Lines of UNPOSTED journals are edited and deleted freely.';

CREATE TABLE generaljournalentry (
  recid bigint PRIMARY KEY, ledger bigint NOT NULL, accountingdate date NOT NULL, documentdate date, documentnumber varchar(30),
  journalnumber varchar(20), journalcategory int NOT NULL,      -- LedgerTransType
  postinglayer int NOT NULL DEFAULT 0, subledgervoucher varchar(20), subledgervoucherdataareaid varchar(4),
  fiscalcalendarperiod bigint NOT NULL DEFAULT 0, transferid bigint NOT NULL DEFAULT 0,
  modifieddatetime timestamp NOT NULL, createddatetime timestamp NOT NULL, createdby varchar(20),
  recversion int NOT NULL DEFAULT 1, partition bigint NOT NULL DEFAULT 5637144576
);

CREATE TABLE generaljournalaccountentry (
  recid bigint PRIMARY KEY, generaljournalentry bigint NOT NULL, ledgerdimension bigint NOT NULL, ledgeraccount varchar(500),
  postingtype int NOT NULL,                     -- LedgerPostingType: 14 LedgerJournal, 31 CustBalance, 32 CustRevenue, 41 VendBalance, 71 PurchConsump
  transactioncurrencyamount numeric(32,16) NOT NULL, transactioncurrencycode varchar(3) NOT NULL,
  accountingcurrencyamount numeric(32,16) NOT NULL, reportingcurrencyamount numeric(32,16) NOT NULL DEFAULT 0,
  iscredit int NOT NULL, iscorrection int NOT NULL DEFAULT 0, quantity numeric(32,16) NOT NULL DEFAULT 0, text varchar(1000),
  allocationlevel int NOT NULL DEFAULT 0, paymentreference varchar(30),
  modifieddatetime timestamp NOT NULL, createddatetime timestamp NOT NULL,
  recversion int NOT NULL DEFAULT 1, partition bigint NOT NULL DEFAULT 5637144576
);
COMMENT ON COLUMN generaljournalaccountentry.accountingcurrencyamount IS 'Credits are negative.';

CREATE TABLE srsanalysisenums (
  enumname varchar(40) NOT NULL, enumitemname varchar(40) NOT NULL, enumitemvalue int NOT NULL,
  enumitemlabel varchar(100), languageid varchar(7) NOT NULL DEFAULT 'en-us',
  recid bigint NOT NULL UNIQUE, recversion int NOT NULL DEFAULT 1, partition bigint NOT NULL DEFAULT 5637144576,
  PRIMARY KEY (enumname, enumitemvalue, languageid)
);
COMMENT ON TABLE srsanalysisenums IS 'Enum labels for reporting. Not every enum is present (only those used in reporting perspectives).';
