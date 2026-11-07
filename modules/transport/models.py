"""Transport: vehicles, drivers and attendants, routes with ordered stops, zones and riders.

- TransportVehicle   a bus or van: registration, capacity, insurance / permit / fitness dates, GPS id
- TransportCrew      a driver or attendant (linked to a staff record where possible), licence expiry
- TransportZone      a fare zone with its fee (stops may carry their own fee instead)
- TransportRoute     a route with its vehicle, driver and attendant
- TransportStop      an ordered stop on a route: pickup and drop times, distance, zone / fee
- TransportRider     a learner allocated to a route and stop for a date range
- TransportTripMark  optional trip attendance: boarded or not, per day and trip
"""
from sqlalchemy import (Boolean, Column, Date, DateTime, ForeignKey, Index, Integer, Numeric, String, Text, Time,
                        UniqueConstraint, text)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from core.db import Base


def _uuid():
    return Column(UUID(as_uuid=True), unique=True, nullable=False, server_default=text("gen_random_uuid()"))


def _company():
    return Column(Integer, ForeignKey("companies.id"), nullable=False)


def _stamps():
    return (Column(DateTime(timezone=True), server_default=func.now()),
            Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now()))


class TransportVehicle(Base):
    __tablename__ = "transport_vehicles"
    __table_args__ = (UniqueConstraint("company_id", "registration_no", name="uq_transport_vehicles_reg"),
                      Index("ix_transport_vehicles_company", "company_id"))
    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    registration_no = Column(String(30), nullable=False)
    name = Column(String(60), nullable=True)  # "Bus 4"
    kind = Column(String(20), nullable=False, server_default=text("'bus'"))  # bus | minibus | van | car | auto
    make_model = Column(String(120), nullable=True)
    capacity = Column(Integer, nullable=False, server_default=text("40"))
    fuel = Column(String(20), nullable=True)
    ownership = Column(String(20), nullable=False, server_default=text("'own'"))  # own | contract
    contractor = Column(String(200), nullable=True)
    insurance_expiry = Column(Date, nullable=True)
    permit_expiry = Column(Date, nullable=True)
    fitness_expiry = Column(Date, nullable=True)
    pollution_expiry = Column(Date, nullable=True)
    gps_device_id = Column(String(80), nullable=True)
    reminded_on = Column(Date, nullable=True)
    notes = Column(Text, nullable=True)
    active = Column(Boolean, nullable=False, server_default=text("true"))
    created_at, updated_at = _stamps()


class TransportCrew(Base):
    __tablename__ = "transport_crew"
    __table_args__ = (Index("ix_transport_crew_company", "company_id", "kind"),)
    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    kind = Column(String(20), nullable=False, server_default=text("'driver'"))  # driver | attendant
    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=True)
    name = Column(String(200), nullable=False)
    phone = Column(String(20), nullable=True)
    licence_no = Column(String(40), nullable=True)
    licence_expiry = Column(Date, nullable=True)
    badge_no = Column(String(40), nullable=True)
    photo_url = Column(Text, nullable=True)
    reminded_on = Column(Date, nullable=True)
    notes = Column(Text, nullable=True)
    active = Column(Boolean, nullable=False, server_default=text("true"))
    created_at, updated_at = _stamps()

    employee = relationship("Employee")


class TransportZone(Base):
    __tablename__ = "transport_zones"
    __table_args__ = (UniqueConstraint("company_id", "name", name="uq_transport_zones_name"),
                      Index("ix_transport_zones_company", "company_id"))
    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    name = Column(String(80), nullable=False)
    fee = Column(Numeric(12, 2), nullable=False, server_default=text("0"))
    description = Column(Text, nullable=True)
    active = Column(Boolean, nullable=False, server_default=text("true"))
    created_at, updated_at = _stamps()


class TransportRoute(Base):
    __tablename__ = "transport_routes"
    __table_args__ = (UniqueConstraint("company_id", "name", name="uq_transport_routes_name"),
                      Index("ix_transport_routes_company", "company_id"))
    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    name = Column(String(120), nullable=False)
    code = Column(String(20), nullable=True)
    vehicle_id = Column(Integer, ForeignKey("transport_vehicles.id"), nullable=True)
    driver_id = Column(Integer, ForeignKey("transport_crew.id"), nullable=True)
    attendant_id = Column(Integer, ForeignKey("transport_crew.id"), nullable=True)
    start_point = Column(String(200), nullable=True)
    capacity = Column(Integer, nullable=True)  # overrides the vehicle's seats
    notes = Column(Text, nullable=True)
    active = Column(Boolean, nullable=False, server_default=text("true"))
    created_at, updated_at = _stamps()

    vehicle = relationship("TransportVehicle")
    driver = relationship("TransportCrew", foreign_keys=[driver_id])
    attendant = relationship("TransportCrew", foreign_keys=[attendant_id])
    stops = relationship("TransportStop", back_populates="route", order_by="TransportStop.sequence")


class TransportStop(Base):
    __tablename__ = "transport_stops"
    __table_args__ = (Index("ix_transport_stops_company", "company_id", "route_id", "sequence"),)
    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    route_id = Column(Integer, ForeignKey("transport_routes.id"), nullable=False)
    name = Column(String(120), nullable=False)
    sequence = Column(Integer, nullable=False, server_default=text("1"))
    pickup_time = Column(Time, nullable=True)
    drop_time = Column(Time, nullable=True)
    distance_km = Column(Numeric(6, 2), nullable=True)
    zone_id = Column(Integer, ForeignKey("transport_zones.id"), nullable=True)
    fee = Column(Numeric(12, 2), nullable=True)  # overrides the zone fee
    landmark = Column(String(200), nullable=True)
    active = Column(Boolean, nullable=False, server_default=text("true"))
    created_at, updated_at = _stamps()

    route = relationship("TransportRoute", back_populates="stops")
    zone = relationship("TransportZone")


class TransportRider(Base):
    __tablename__ = "transport_riders"
    __table_args__ = (Index("ix_transport_riders_company_route", "company_id", "route_id", "status"),
                      Index("ix_transport_riders_student", "company_id", "student_id"))
    id = Column(Integer, primary_key=True)
    uuid = _uuid()
    company_id = _company()
    student_id = Column(Integer, ForeignKey("students.id"), nullable=False)
    route_id = Column(Integer, ForeignKey("transport_routes.id"), nullable=False)
    stop_id = Column(Integer, ForeignKey("transport_stops.id"), nullable=False)
    direction = Column(String(10), nullable=False, server_default=text("'both'"))  # both | pickup | drop
    start_date = Column(Date, nullable=False)
    end_date = Column(Date, nullable=True)
    status = Column(String(20), nullable=False, server_default=text("'active'"))  # active | ended
    fee_amount = Column(Numeric(12, 2), nullable=True)
    fee_ref = Column(String(80), nullable=True)  # charge in fees when billed
    notes = Column(Text, nullable=True)
    created_at, updated_at = _stamps()

    student = relationship("Student")
    route = relationship("TransportRoute")
    stop = relationship("TransportStop")


class TransportTripMark(Base):
    __tablename__ = "transport_trip_marks"
    __table_args__ = (UniqueConstraint("route_id", "day", "trip", "student_id", name="uq_transport_trip_marks"),
                      Index("ix_transport_trip_marks_company", "company_id", "route_id", "day"))
    id = Column(Integer, primary_key=True)
    company_id = _company()
    route_id = Column(Integer, ForeignKey("transport_routes.id"), nullable=False)
    student_id = Column(Integer, ForeignKey("students.id"), nullable=False)
    day = Column(Date, nullable=False)
    trip = Column(String(10), nullable=False)  # pickup | drop
    boarded = Column(Boolean, nullable=False)
    marked_by = Column(String(200), nullable=True)
    marked_at = Column(DateTime(timezone=True), server_default=func.now())


TABLES = ["transport_vehicles", "transport_crew", "transport_zones", "transport_routes", "transport_stops",
          "transport_riders", "transport_trip_marks"]
