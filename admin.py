from django.contrib import admin
from .models import Movie, Theater, Show, Seat, Booking

@admin.register(Movie)
class MovieAdmin(admin.ModelAdmin):
    list_display = ('title', 'release_date')

@admin.register(Theater)
class TheaterAdmin(admin.ModelAdmin):
    list_display = ('name', 'location')

@admin.register(Show)
class ShowAdmin(admin.ModelAdmin):
    list_display = ('movie', 'theater', 'show_date', 'show_time')

@admin.register(Seat)
class SeatAdmin(admin.ModelAdmin):
    list_display = ('show', 'seat_number', 'row', 'price', 'is_reserved')

@admin.register(Booking)
class BookingAdmin(admin.ModelAdmin):
    list_display = ('user', 'show', 'booking_time', 'payment_status')
    filter_horizontal = ('seats',)
