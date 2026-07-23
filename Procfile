# iSupply Scan — licencni server (Railway)
#
# PROC TAHLE PODOBA:
#   Puvodne tu bylo jen "gunicorn app:app --bind ... --timeout 120", coz znamena
#   VYCHOZI nastaveni gunicornu: 1 worker, 1 vlakno = server obslouzi JEDEN
#   pozadavek naraz. Pri 500 aktivnich klientech to nestaci ani na samotne
#   heartbeaty (500 klientu / 60 s = ~8 pozadavku za sekundu), natoz na
#   autorizaci skenu.
#
#   --workers 4 --threads 8  = az 32 soubeznych pozadavku.
#   Stav aplikace je cely v PostgreSQL (zadne promenne v pameti mezi requesty),
#   takze vic workeru nic nerozbije.
#
#   --timeout 60 misto 120: zaseknuty pozadavek drive blokoval worker dve
#   minuty. Autorizace skenu ma bezet v desetinach sekundy, minuta je uz
#   projev poruchy.
#
#   POZOR na pocet spojeni do databaze: 4 x 8 = az 32 soubeznych spojeni.
#   Kdyz budes zvedat workery vys, zkontroluj max_connections v Postgresu
#   (Railway ma ve vychozim nastaveni dost, ale neni to bez limitu).
web: gunicorn app:app --bind 0.0.0.0:$PORT --workers 4 --threads 8 --timeout 60 --graceful-timeout 30 --access-logfile - --error-logfile -
