from django.shortcuts import render, get_object_or_404, redirect
from django.urls import reverse
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.core.mail import send_mail
from django.conf import settings
from django.http import JsonResponse, HttpResponseForbidden, HttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.utils import timezone
from django.core.paginator import Paginator, EmptyPage, PageNotAnInteger
from django.contrib import messages
from django.contrib.auth import logout as auth_logout
from .models import Movie, Show, Seat, Booking
from .forms import RegisterForm
import razorpay
import json
import hmac
import hashlib
import logging
import threading
import time

logger = logging.getLogger(__name__)

client = razorpay.Client(auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET))

api_failure_count = 0
alert_sent = False


def api_call_handler(success):
    """Track API failures and send one alert email after 10 consecutive failures."""
    global api_failure_count, alert_sent

    if success:
        api_failure_count = 0
        alert_sent = False
        return

    api_failure_count += 1
    if api_failure_count >= 10 and not alert_sent:
        send_mail(
            subject='API Alert: Consecutive failures detected',
            message='The booking API has failed 10 consecutive times.',
            from_email=getattr(settings, 'DEFAULT_FROM_EMAIL', 'noreply@movieflix.local'),
            recipient_list=[getattr(settings, 'ADMIN_ALERT_EMAIL', 'admin@movieflix.local')],
            fail_silently=True,
        )
        alert_sent = True

# ✅ Home Page
def home(request):
    today = timezone.now().date()
    shows_today = Show.objects.filter(show_date=today)
    popular_movies = Movie.objects.all()[:5]  # Placeholder for popular movies
    context = {
        'shows_today': shows_today,
        'popular_movies': popular_movies,
    }
    return render(request, 'booking/home.html', context)

# ✅ Static Pages
def about(request):
    return render(request, 'booking/about.html')

def contact(request):
    return render(request, 'booking/contact.html')

# ✅ Movie List with Pagination
def movie_list(request):
    movies = Movie.objects.all()
    paginator = Paginator(movies, 10)  # 10 movies per page
    page = request.GET.get('page')
    try:
        movies_page = paginator.page(page)
    except PageNotAnInteger:
        movies_page = paginator.page(1)
    except EmptyPage:
        movies_page = paginator.page(paginator.num_pages)

    context = {
        'movies': movies_page,
    }
    return render(request, 'booking/movie_list.html', context)

# ✅ Movie Details
def movie_detail(request, movie_id):
    movie = get_object_or_404(Movie, pk=movie_id)
    return render(request, 'booking/movie_detail.html', {'movie': movie})

# ✅ User Profile (Booking History with Dynamic Total Price)
@login_required
def user_profile(request):
    bookings = Booking.objects.filter(user=request.user).order_by('-booking_time')

    # ✅ Calculate total price dynamically based on seats booked
    for booking in bookings:
        booking.total_price = sum(seat.price for seat in booking.seats.all())

    return render(request, 'booking/profile.html', {'bookings': bookings})

# ✅ Track seat reservations
seat_reservation_times = {}

def release_seat_after_timeout(seat_id, timeout=300):
    """Release seat if not booked within timeout (5 minutes default)."""
    time.sleep(timeout)
    try:
        seat = Seat.objects.get(id=seat_id)
        if seat.is_reserved:
            seat.is_reserved = False
            seat.save()
            seat_reservation_times.pop(seat_id, None)
    except Seat.DoesNotExist:
        pass

# ✅ Booking View
@login_required
def booking(request, show_id):
    show = get_object_or_404(Show, pk=show_id)
    seats = Seat.objects.filter(show=show, is_reserved=False)
    
    if request.method == 'POST':
        raw_ids = request.POST.getlist('seats')
        if len(raw_ids) == 1 and ',' in raw_ids[0]:
            selected_seat_ids = [seat_id.strip() for seat_id in raw_ids[0].split(',') if seat_id.strip()]
        else:
            selected_seat_ids = [seat_id.strip() for seat_id in raw_ids if seat_id.strip()]

        selected_seats = Seat.objects.filter(show=show, id__in=selected_seat_ids, is_reserved=False)

        if not selected_seats.exists():
            return render(request, 'booking/booking.html', {'show': show, 'seats': seats, 'error': 'No seats selected or seats already reserved.'})

        # ✅ Reserve seats temporarily
        for seat in selected_seats:
            seat.is_reserved = True
            seat.save()
            seat_reservation_times[seat.id] = time.time()
            threading.Thread(target=release_seat_after_timeout, args=(seat.id,), daemon=True).start()

        amount = int(sum(seat.price for seat in selected_seats) * 100)  # ✅ Amount in paise for Razorpay

        try:
            # ✅ Create Razorpay order
            razorpay_order = client.order.create(dict(amount=amount, currency='INR', payment_capture='0'))

            booking = Booking.objects.create(
                user=request.user,
                show=show,
                payment_status='Pending',
                razorpay_order_id=razorpay_order['id']
            )
            booking.seats.set(selected_seats)
            booking.save()

            context = {
                'booking': booking,
                'razorpay_key_id': settings.RAZORPAY_KEY_ID,
                'amount': amount,
                'display_amount': amount / 100,
                'currency': 'INR',
                'callback_url': request.build_absolute_uri(reverse('booking:payment_success')) + f'?booking_id={booking.id}',
            }
            return render(request, 'booking/payment.html', context)

        except razorpay.errors.BadRequestError as e:
            print(f"Razorpay BadRequestError: {str(e)}")
            return render(request, 'booking/booking.html', {'show': show, 'seats': seats, 'error': 'Unable to process booking. Please try again later.'})

    return render(request, 'booking/booking.html', {'show': show, 'seats': seats})

# ✅ Payment Success
@login_required
def payment_success(request):
    booking_id = request.GET.get('booking_id')
    if booking_id:
        try:
            booking = Booking.objects.get(id=booking_id, user=request.user)
            booking.payment_status = 'Success'
            booking.save()
            messages.success(request, f"Your payment was successful! Your booking for {booking.show.movie.title} is confirmed.")
        except Booking.DoesNotExist:
            messages.success(request, "Your payment was successful! Enjoy the movie.")
    else:
        messages.success(request, "Your payment was successful! Enjoy the movie.")
    return render(request, 'booking/payment_success.html')

# ✅ Payment Failure
@login_required
def payment_failure(request):
    booking_id = request.GET.get('booking_id')
    if booking_id:
        try:
            booking = Booking.objects.get(id=booking_id, user=request.user)
            booking.payment_status = 'Failed'
            booking.save()
            # ✅ Release reserved seats if payment fails
            for seat in booking.seats.all():
                seat.is_reserved = False
                seat.save()
            messages.error(request, f"Your payment for {booking.show.movie.title} failed. Please try again.")
        except Booking.DoesNotExist:
            messages.error(request, "Your payment failed. Please try again or contact support.")
    else:
        messages.error(request, "Your payment failed. Please try again or contact support.")
    return render(request, 'booking/payment_failure.html')

# ✅ Razorpay Webhook for automatic updates
@csrf_exempt
def razorpay_webhook(request):
    if request.method == 'POST':
        webhook_secret = settings.RAZORPAY_WEBHOOK_SECRET
        received_signature = request.headers.get('X-Razorpay-Signature')
        body = request.body.decode('utf-8')

        # ✅ Verify webhook signature
        generated_signature = hmac.new(
            webhook_secret.encode(),
            body.encode(),
            hashlib.sha256
        ).hexdigest()

        if not hmac.compare_digest(generated_signature, received_signature):
            logger.warning("Invalid Razorpay webhook signature")
            return HttpResponseForbidden("Invalid signature")

        data = json.loads(body)
        event = data.get('event')

        if event == 'payment.captured':
            razorpay_order_id = data['payload']['payment']['entity']['order_id']
            payment_id = data['payload']['payment']['entity']['id']
            try:
                booking = Booking.objects.get(razorpay_order_id=razorpay_order_id)
                booking.payment_status = 'Success'
                booking.razorpay_payment_id = payment_id
                booking.save()
                try:
                    send_mail(
                        'Booking Confirmed',
                        f'Your booking for {booking.show.movie.title} is confirmed.',
                        settings.DEFAULT_FROM_EMAIL,
                        [booking.user.email],
                        fail_silently=False,
                    )
                except Exception as e:
                    logger.error(f"Error sending confirmation email: {e}")
            except Booking.DoesNotExist:
                logger.error(f"Booking with order id {razorpay_order_id} does not exist")

        elif event == 'payment.failed':
            razorpay_order_id = data['payload']['payment']['entity']['order_id']
            try:
                booking = Booking.objects.get(razorpay_order_id=razorpay_order_id)
                booking.payment_status = 'Failed'
                booking.save()
                try:
                    send_mail(
                        'Booking Payment Failed',
                        f'Your payment for {booking.show.movie.title} failed. Please try again.',
                        settings.DEFAULT_FROM_EMAIL,
                        [booking.user.email],
                        fail_silently=False,
                    )
                except Exception as e:
                    logger.error(f"Error sending failure email: {e}")
                for seat in booking.seats.all():
                    seat.is_reserved = False
                    seat.save()
            except Booking.DoesNotExist:
                logger.error(f"Booking with order id {razorpay_order_id} does not exist")

        return HttpResponse(status=200)
    else:
        return HttpResponseForbidden()

# ✅ Register View
def register(request):
    if request.method == 'POST':
        form = RegisterForm(request.POST)
        if form.is_valid():
            user = form.save()
            return redirect('booking:login')
    else:
        form = RegisterForm()
    return render(request, 'booking/register.html', {'form': form})

# ✅ Custom Logout (FIXED)
def custom_logout(request):
    """Logs out the user and redirects to login page."""
    auth_logout(request)
    return redirect('booking:login')
