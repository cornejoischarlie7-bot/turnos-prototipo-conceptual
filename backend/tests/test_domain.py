"""Reglas puras de spec.md §3, sin base de datos."""
from datetime import datetime, timedelta, timezone

import pytest

from domain import (ErrorDominio, de_iso, dentro_de_horario, solapa, validar_cancelacion, validar_contacto,
                    validar_inicio)

UTC = timezone.utc
AHORA = datetime(2026, 11, 4, 13, 0, tzinfo=UTC)  # miércoles 10:00 ART
H = timedelta(hours=1)


def t(h, m=0, dia=4):
    """Instante del 2026-11-<dia> a las h:m en la zona de operación (UTC-3)."""
    return datetime(2026, 11, dia, h + 3, m, tzinfo=UTC)


class TestSolapamiento:  # RN-02
    def test_solapados(self):
        assert solapa(t(10), t(10, 30), t(10, 15), t(10, 45))

    def test_contiguos_no_solapan(self):
        assert not solapa(t(10), t(10, 30), t(10, 30), t(11))

    def test_contenido(self):
        assert solapa(t(10), t(12), t(10, 30), t(11))


class TestCancelacion:  # RN-01 / RN-06
    def test_cliente_justo_2h_puede(self):
        assert validar_cancelacion("CONFIRMADO", AHORA + 2 * H, AHORA, "CLIENTE", None) is None

    def test_cliente_motivo_opcional(self):
        assert validar_cancelacion("CONFIRMADO", AHORA + 3 * H, AHORA, "CLIENTE", " Me surgió algo ") == "Me surgió algo"

    def test_cliente_1h59_no_puede(self):
        with pytest.raises(ErrorDominio) as e:
            validar_cancelacion("CONFIRMADO", AHORA + timedelta(hours=1, minutes=59), AHORA, "CLIENTE", None)
        assert e.value.codigo == "CANCELLATION_WINDOW_EXPIRED" and e.value.http == 409

    def test_profesional_fuera_de_ventana_con_motivo(self):
        assert validar_cancelacion("CONFIRMADO", AHORA + H, AHORA, "PROFESIONAL", " Viaje ") == "Viaje"

    def test_admin_sin_motivo(self):
        with pytest.raises(ErrorDominio) as e:
            validar_cancelacion("CONFIRMADO", AHORA + 5 * H, AHORA, "ADMIN", "  ")
        assert e.value.codigo == "REASON_REQUIRED"

    @pytest.mark.parametrize("estado", ["CANCELADO", "COMPLETADO", "AUSENTE"])
    def test_estados_terminales(self, estado):
        with pytest.raises(ErrorDominio) as e:
            validar_cancelacion(estado, AHORA + 5 * H, AHORA, "ADMIN", "x")
        assert e.value.codigo == "INVALID_STATE_TRANSITION"

    def test_turno_iniciado(self):
        with pytest.raises(ErrorDominio) as e:
            validar_cancelacion("CONFIRMADO", AHORA, AHORA, "ADMIN", "x")
        assert e.value.codigo == "INVALID_STATE_TRANSITION"


class TestContacto:  # RN-03
    def test_valido(self):
        assert validar_contacto("Lucía Fernández", "lucia@mail.test", "+5491155550101") == {}

    def test_invalido(self):
        errores = validar_contacto("L", "sin-arroba", "1155550101")
        assert set(errores) == {"nombre", "email", "telefono"}


class TestInicio:  # RN-04
    def test_valido(self):
        validar_inicio(AHORA + 2 * H, AHORA)

    @pytest.mark.parametrize("inicio", [
        AHORA + 2 * H + timedelta(minutes=10),   # fuera de grilla
        AHORA + timedelta(minutes=45),           # antelación < 1 h
        AHORA + timedelta(days=61),              # horizonte > 60 días
    ])
    def test_invalido(self, inicio):
        with pytest.raises(ErrorDominio) as e:
            validar_inicio(inicio, AHORA)
        assert e.value.codigo == "INVALID_TIME_RANGE"

    def test_sin_zona_horaria(self):
        with pytest.raises(ErrorDominio) as e:
            de_iso("2026-11-06T10:00:00")
        assert e.value.codigo == "VALIDATION_ERROR"


HORARIO = [(d, "09:00", "13:00") for d in range(1, 6)]


class TestHorario:
    def test_dentro(self):
        assert dentro_de_horario(t(9, dia=6), t(9, 30, dia=6), HORARIO)

    def test_termina_justo_al_cierre(self):
        assert dentro_de_horario(t(12, 30, dia=6), t(13, dia=6), HORARIO)

    def test_pasa_el_cierre(self):
        assert not dentro_de_horario(t(12, 45, dia=6), t(13, 15, dia=6), HORARIO)

    def test_sabado(self):
        assert not dentro_de_horario(t(10, dia=7), t(10, 30, dia=7), HORARIO)
