--
-- PostgreSQL database dump
--

\restrict EXjN3A3x6SCsuKObNmkeudQoXMLOKlK6v65bV1GnlgiSZ8lbAfXMAuS3xXJe87d

-- Dumped from database version 17.6
-- Dumped by pg_dump version 17.6

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET transaction_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Name: part_status; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.part_status AS ENUM (
    'stocked',
    'reserved',
    'out'
);


--
-- Name: mark_location_stocked(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.mark_location_stocked() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
  UPDATE locations
     SET state = 'Stocked'
   WHERE position_code = NEW.part_cataloged_position
     AND state <> 'Stocked';
  RETURN NEW;
END;
$$;


--
-- Name: prevent_negative_stock(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.prevent_negative_stock() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
DECLARE
  current_qty int;
BEGIN
  IF NEW.quantity_delta < 0 THEN
    SELECT COALESCE(SUM(quantity_delta), 0) INTO current_qty
    FROM public.movements
    WHERE part_id = NEW.part_id
      AND position_code = NEW.position_code;

    IF current_qty + NEW.quantity_delta < 0 THEN
      RAISE EXCEPTION 'Insufficient stock for part_id % at bin %, available %, requested %',
        NEW.part_id, NEW.position_code, current_qty, -NEW.quantity_delta;
    END IF;
  END IF;

  RETURN NEW;
END$$;


--
-- Name: set_updated_at(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.set_updated_at() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
  NEW.updated_at = now();
  RETURN NEW;
END; $$;


SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: categories; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.categories (
    category_id text NOT NULL,
    source_name text
);


--
-- Name: intakes; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.intakes (
    intake_id bigint NOT NULL,
    part_id bigint NOT NULL,
    quantity_scanned integer NOT NULL,
    unit_price numeric(12,4),
    currency text DEFAULT 'USD'::text,
    invoice_number text,
    sales_order text,
    lot_code text,
    date_code text,
    customer_reference text,
    digikey_part_number text,
    manufacturer_part_number text,
    packing_list_number text,
    country_of_origin text,
    label_type text,
    internal_part_id text,
    raw_scan_fields jsonb DEFAULT '{}'::jsonb NOT NULL,
    part_cataloged_position text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT intakes_quantity_scanned_check CHECK ((quantity_scanned >= 0))
);


--
-- Name: intakes_intake_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.intakes_intake_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: intakes_intake_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.intakes_intake_id_seq OWNED BY public.intakes.intake_id;


--
-- Name: locations; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.locations (
    position_code text NOT NULL,
    description text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    state text DEFAULT 'Available'::text NOT NULL,
    CONSTRAINT locations_state_check CHECK ((state = ANY (ARRAY['Available'::text, 'Reserved'::text, 'Stocked'::text, 'Checked out'::text])))
);


--
-- Name: movements; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.movements (
    movement_id bigint NOT NULL,
    part_id bigint NOT NULL,
    position_code text NOT NULL,
    quantity_delta integer NOT NULL,
    unit_price numeric(12,4),
    movement_type text NOT NULL,
    lot_code text,
    reference_doc text,
    note text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT movements_movement_type_check CHECK ((movement_type = ANY (ARRAY['intake'::text, 'transfer'::text, 'consumption'::text, 'adjustment'::text])))
);


--
-- Name: movements_movement_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.movements_movement_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: movements_movement_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.movements_movement_id_seq OWNED BY public.movements.movement_id;


--
-- Name: parts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.parts (
    part_id bigint NOT NULL,
    mpn text NOT NULL,
    manufacturer text,
    description text,
    detailed_description text,
    product_url text,
    datasheet_url text,
    image_url text,
    unit_price numeric(12,4),
    product_status text,
    lifecycle_active boolean,
    lifecycle_obsolete boolean,
    category_id text,
    category_source_name text,
    attributes jsonb DEFAULT '{}'::jsonb NOT NULL,
    unknown_parameters jsonb DEFAULT '{}'::jsonb NOT NULL,
    raw_vendor_json jsonb,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: parts_part_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.parts_part_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: parts_part_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.parts_part_id_seq OWNED BY public.parts.part_id;


--
-- Name: v_current_inventory; Type: VIEW; Schema: public; Owner: -
--

CREATE VIEW public.v_current_inventory AS
 SELECT i.part_id,
    p.mpn,
    i.part_cataloged_position AS position_code,
    sum(i.quantity_scanned) AS quantity
   FROM (public.intakes i
     JOIN public.parts p ON ((p.part_id = i.part_id)))
  GROUP BY i.part_id, p.mpn, i.part_cataloged_position;


--
-- Name: v_inventory_available; Type: VIEW; Schema: public; Owner: -
--

CREATE VIEW public.v_inventory_available AS
 SELECT part_id,
    position_code,
    (sum(quantity_delta))::integer AS qty_on_hand
   FROM public.movements
  WHERE (position_code !~~ 'OUT%'::text)
  GROUP BY part_id, position_code;


--
-- Name: v_inventory_on_loan; Type: VIEW; Schema: public; Owner: -
--

CREATE VIEW public.v_inventory_on_loan AS
 SELECT part_id,
    position_code,
    (sum(quantity_delta))::integer AS qty_on_loan
   FROM public.movements
  WHERE (position_code ~~ 'OUT%'::text)
  GROUP BY part_id, position_code;


--
-- Name: v_inventory_totals; Type: VIEW; Schema: public; Owner: -
--

CREATE VIEW public.v_inventory_totals AS
 WITH avail AS (
         SELECT v_inventory_available.part_id,
            (sum(v_inventory_available.qty_on_hand))::integer AS available
           FROM public.v_inventory_available
          GROUP BY v_inventory_available.part_id
        ), loan AS (
         SELECT v_inventory_on_loan.part_id,
            (sum(v_inventory_on_loan.qty_on_loan))::integer AS on_loan
           FROM public.v_inventory_on_loan
          GROUP BY v_inventory_on_loan.part_id
        )
 SELECT p.part_id,
    COALESCE(avail.available, 0) AS available,
    COALESCE(loan.on_loan, 0) AS on_loan,
    (COALESCE(avail.available, 0) + COALESCE(loan.on_loan, 0)) AS owned
   FROM ((public.parts p
     LEFT JOIN avail ON ((avail.part_id = p.part_id)))
     LEFT JOIN loan ON ((loan.part_id = p.part_id)));


--
-- Name: intakes intake_id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.intakes ALTER COLUMN intake_id SET DEFAULT nextval('public.intakes_intake_id_seq'::regclass);


--
-- Name: movements movement_id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.movements ALTER COLUMN movement_id SET DEFAULT nextval('public.movements_movement_id_seq'::regclass);


--
-- Name: parts part_id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.parts ALTER COLUMN part_id SET DEFAULT nextval('public.parts_part_id_seq'::regclass);


--
-- Name: categories categories_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.categories
    ADD CONSTRAINT categories_pkey PRIMARY KEY (category_id);


--
-- Name: intakes intakes_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.intakes
    ADD CONSTRAINT intakes_pkey PRIMARY KEY (intake_id);


--
-- Name: locations locations_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.locations
    ADD CONSTRAINT locations_pkey PRIMARY KEY (position_code);


--
-- Name: movements movements_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.movements
    ADD CONSTRAINT movements_pkey PRIMARY KEY (movement_id);


--
-- Name: parts parts_mpn_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.parts
    ADD CONSTRAINT parts_mpn_key UNIQUE (mpn);


--
-- Name: parts parts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.parts
    ADD CONSTRAINT parts_pkey PRIMARY KEY (part_id);


--
-- Name: idx_intakes_part; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_intakes_part ON public.intakes USING btree (part_id);


--
-- Name: idx_intakes_position; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_intakes_position ON public.intakes USING btree (part_cataloged_position);


--
-- Name: idx_locations_state; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_locations_state ON public.locations USING btree (state);


--
-- Name: idx_movements_part_pos; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_movements_part_pos ON public.movements USING btree (part_id, position_code);


--
-- Name: idx_movements_part_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_movements_part_time ON public.movements USING btree (part_id, created_at DESC);


--
-- Name: idx_movements_pos; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_movements_pos ON public.movements USING btree (position_code);


--
-- Name: idx_parts_attributes_gin; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_parts_attributes_gin ON public.parts USING gin (attributes);


--
-- Name: idx_parts_category; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_parts_category ON public.parts USING btree (category_id);


--
-- Name: idx_parts_unknown_params_gin; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_parts_unknown_params_gin ON public.parts USING gin (unknown_parameters);


--
-- Name: intakes trg_intakes_mark_stocked; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_intakes_mark_stocked AFTER INSERT ON public.intakes FOR EACH ROW EXECUTE FUNCTION public.mark_location_stocked();


--
-- Name: parts trg_parts_updated_at; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_parts_updated_at BEFORE UPDATE ON public.parts FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();


--
-- Name: movements trg_prevent_negative_stock; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_prevent_negative_stock BEFORE INSERT ON public.movements FOR EACH ROW EXECUTE FUNCTION public.prevent_negative_stock();


--
-- Name: intakes intakes_part_cataloged_position_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.intakes
    ADD CONSTRAINT intakes_part_cataloged_position_fkey FOREIGN KEY (part_cataloged_position) REFERENCES public.locations(position_code);


--
-- Name: intakes intakes_part_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.intakes
    ADD CONSTRAINT intakes_part_id_fkey FOREIGN KEY (part_id) REFERENCES public.parts(part_id) ON DELETE CASCADE;


--
-- Name: movements movements_part_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.movements
    ADD CONSTRAINT movements_part_id_fkey FOREIGN KEY (part_id) REFERENCES public.parts(part_id) ON DELETE CASCADE;


--
-- Name: movements movements_position_code_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.movements
    ADD CONSTRAINT movements_position_code_fkey FOREIGN KEY (position_code) REFERENCES public.locations(position_code);


--
-- Name: parts parts_category_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.parts
    ADD CONSTRAINT parts_category_id_fkey FOREIGN KEY (category_id) REFERENCES public.categories(category_id);


--
-- PostgreSQL database dump complete
--

\unrestrict EXjN3A3x6SCsuKObNmkeudQoXMLOKlK6v65bV1GnlgiSZ8lbAfXMAuS3xXJe87d

