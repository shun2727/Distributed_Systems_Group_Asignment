// Configure this URL if the Booking service runs somewhere other than localhost:5001.
const requestedBookingPort = Number(new URLSearchParams(window.location.search).get('booking_port'));
const bookingPort = Number.isInteger(requestedBookingPort) && requestedBookingPort > 0 && requestedBookingPort <= 65535 ? requestedBookingPort : 5001;
const API_BASE = window.HOTEL_BOOKING_API || `${window.location.protocol}//${window.location.hostname}:${bookingPort}`;
const state = {
    token: null,
    adminToken: null,
    authMode: 'user',
    phoneNumber: null,
    rooms: [],
    bookings: [],
    selectedRoom: null,
    pending: null,
    cancelTarget: null,
    adminCancelTarget: null,
    adminModifyTarget: null,
    adminAvailableRooms: [],
    adminSelectedRoom: null,
    pollers: new Map()
};
const ROOM_PROFILES = {
    single: {
        name: 'Superior Double Room', bed: '1 double bed', bathrooms: 1, area: 'about 25 m²',
        image: '/assets/superior%20room%20with%201%20double%20bed.jpg',
        bathroomImage: '/assets/bathroom.jpg',
        amenities: ['Wi-Fi', 'Air conditioning', '55-inch TV', 'In-room safe', 'Bathroom amenities', 'Kettle, tea and coffee', 'Mini refrigerator']
    },
    double: {
        name: 'Superior Twin Room', bed: '2 single beds', bathrooms: 1, area: 'about 25 m²',
        image: '/assets/superior%20room%20with%202%20single%20beds.jpg',
        bathroomImage: '/assets/bathroom(2).jpg',
        amenities: ['Wi-Fi', 'Air conditioning', '55-inch TV', 'In-room safe', 'Bathroom amenities', 'Kettle, tea and coffee', 'Mini refrigerator']
    },
    king_suite: {
        name: 'Deluxe Twin Room', bed: '2 single beds', bathrooms: 1, area: 'about 25 m²',
        image: '/assets/deluxe%20room%20with%202%20single%20beds(2).jpg',
        bathroomImage: '/assets/bathroom(2).jpg',
        amenities: ['Wi-Fi', 'Air conditioning', '55-inch TV', 'In-room safe', 'Bathroom amenities', 'Kettle, tea and coffee', 'Mini refrigerator']
    }
};
const $ = (id) => document.getElementById(id);
const dateInput = $('checkIn');
const localDate = new Date();
const dateString = (d) => `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`;
dateInput.min = dateString(localDate);
dateInput.value = dateString(localDate);
const tomorrow = new Date(localDate);
tomorrow.setDate(tomorrow.getDate() + 1);
$('checkOut').min = dateString(tomorrow);
$('checkOut').value = dateString(tomorrow);
dateInput.addEventListener('change', () => {
    const d = new Date(`${dateInput.value}T12:00:00`);
    d.setDate(d.getDate() + 1);
    $('checkOut').min = dateString(d);
    if ($('checkOut').value <= dateInput.value) $('checkOut').value = dateString(d)
});

function message(el, text, type = 'info') {
    el.textContent = text;
    el.className = `message ${type}`;
    el.hidden = false
}

function clearMessage(el) {
    el.hidden = true;
    el.textContent = ''
}

function errorText(data, fallback = 'Something went wrong. Please try again.') {
    return data?.error?.message || fallback
}
async function api(path, {
    method = 'GET',
    body,
    auth = true,
    key
} = {}) {
    const headers = {};
    if (body !== undefined) headers['Content-Type'] = 'application/json';
    if (auth && state.token) headers['X-Customer-Token'] = state.token;
    if (auth && state.adminToken) headers['X-Admin-Token'] = state.adminToken;
    if (key) headers['Idempotency-Key'] = key;
    let response;
    try {
        response = await fetch(`${API_BASE}${path}`, {
            method,
            headers,
            body: body === undefined ? undefined : JSON.stringify(body)
        })
    } catch (e) {
        throw new Error('The booking service could not be reached. Please try again shortly.')
    }
    let data = {};
    try {
        data = await response.json()
    } catch (e) {}
    if (response.status === 401 && auth) {
        state.token = null;
        state.phoneNumber = null;
        renderAccount();
        openAuth();
        throw new Error('Your session expired. Please log in again.')
    }
    if (!response.ok) return {
        ok: false,
        status: response.status,
        data
    };
    return {
        ok: true,
        status: response.status,
        data
    };
}

function switchTab(tab) {
    if (tab === 'bookings' && !state.token) {
        openAuth();
        return
    }
    if (tab === 'notifications' && !state.token) { openAuth(); return }
    if (tab === 'admin' && !state.adminToken) { openAuth(); return }
    if (tab === 'adminHistory' && !state.adminToken) { openAuth(); return }
    $('searchTab').hidden = tab !== 'search';
    $('bookingsTab').hidden = tab !== 'bookings';
    $('notificationsTab').hidden = tab !== 'notifications';
    $('adminTab').hidden = tab !== 'admin';
    $('adminHistoryTab').hidden = tab !== 'adminHistory';
    document.querySelectorAll('[data-tab]').forEach(button => button.classList.toggle('active', button.dataset.tab === `${tab}Tab`));
    if (tab === 'bookings') loadBookings();
    if (tab === 'notifications') markNotificationsRead();
    if (tab === 'admin') { loadAdminBookings(); loadAdminDashboardStats() }
    if (tab === 'adminHistory') loadAdminHistory()
}

function renderAccount() {
    if (state.adminToken) {
        document.body.classList.add('admin-mode');
        $('accountName').textContent='Administrator'; $('accountButton').textContent='Sign out'; $('adminNav').hidden=false; $('adminHistoryNav').hidden=false; $('bookingsNav').hidden=true; $('notificationsNav').hidden=true; $('accountNotifications').hidden=true; switchTab('admin'); return
    }
    document.body.classList.remove('admin-mode');
    $('adminNav').hidden=true;
    $('adminHistoryNav').hidden=true;
    if (state.token) {
        $('accountName').textContent = `Hi, ${state.phoneNumber}`;
        $('accountButton').textContent = 'Sign out';
        $('bookingsNav').hidden = false;
        $('notificationsNav').hidden = false;
        $('accountNotifications').hidden = false;
        loadNotifications();
        $('bookingsEmpty').textContent = 'Loading your bookings…';
        loadBookings()
    } else {
        $('accountName').textContent = 'Guest mode';
        $('accountButton').textContent = 'Log in';
        $('bookingsNav').hidden = true;
        $('notificationsNav').hidden = true;
        $('accountNotifications').hidden = true;
        switchTab('search');
        $('bookingsEmpty').textContent = 'Log in to see your bookings.';
        $('bookingList').replaceChildren()
    }
}

function openAuth() {
    $('authOverlay').hidden = false;
    clearMessage($('authMessage'));
    $('authIdentity').focus()
}

function showToast(text) {
    const t = $('toast');
    t.textContent = text;
    t.classList.add('show');
    setTimeout(() => t.classList.remove('show'), 3200)
}

function escapeText(value) {
    return String(value ?? '').replace(/[&<>"']/g, c => ({
        '&': '&amp;',
        '<': '&lt;',
        '>': '&gt;',
        '"': '&quot;',
        "'": '&#39;'
    } [c]))
}

function roomTypeName(type) {
    return ROOM_PROFILES[type]?.name || 'Room'
}

function roomLabel(room) {
    return `${roomTypeName(room.room_type)}${room.room_number?` · Room ${room.room_number}`:''}`
}

function renderRooms(rooms) {
    state.rooms = rooms;
    $('results').hidden = false;
    $('roomList').replaceChildren();
    const type = $('roomType').value;
    const visible = type ? rooms.filter(room => room.room_type === type) : rooms;
    $('resultCount').textContent = `${visible.length} ${visible.length===1?'room':'rooms'} left`;
    const groups = new Map();
    for (const room of visible) {
        if (!groups.has(room.room_type)) groups.set(room.room_type, []);
        groups.get(room.room_type).push(room)
    }
    if (!groups.size) {
        $('roomList').innerHTML = '<div class="empty">No rooms are available for those dates. Try another date range or room type.</div>';
        return
    }
    for (const [roomType, group] of groups) {
        const room = group[0];
        const profile = ROOM_PROFILES[roomType];
        const card = document.createElement('article');
        card.className = 'room-card';
        const image = document.createElement('img');
        image.className = 'room-image';
        image.src = profile?.image || ROOM_PROFILES.single.image;
        image.alt = roomTypeName(roomType);
        image.loading = 'lazy';
        const copy = document.createElement('div');
        copy.className = 'room-copy';
        copy.innerHTML = `<div class="room-name">${escapeText(roomTypeName(roomType))}</div><div class="room-meta">${escapeText(profile?.bed||'Bed details on request')} · Up to ${escapeText(room.capacity)} ${room.capacity===1?'guest':'guests'} · ${profile?.bathrooms||1} bathroom</div><span class="room-count">${group.length} ${group.length===1?'room':'rooms'} left</span>`;
        const actions=document.createElement('div');
        actions.className='room-actions';
        const price=document.createElement('div');
        price.className='room-price room-action-price';
        price.innerHTML=`<span class="room-price-amount">${escapeText(room.currency||'MYR')} ${(Number(room.price_per_night_cents||0)/100).toFixed(2)}</span><small>/ night</small>`;
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'button';
        button.textContent = 'View details & book';
        button.addEventListener('click', () => chooseRoom(room, button));
        actions.append(price,button);
        card.append(image, copy, actions);
        $('roomList').append(card)
    }
}
$('searchForm').addEventListener('submit', async e => {
    e.preventDefault();
    clearMessage($('searchMessage'));
    $('results').hidden = true;
    const ci = $('checkIn').value,
        co = $('checkOut').value;
    if (!ci || !co || co <= ci) {
        message($('searchMessage'), 'Please choose a check-out date after check-in.', 'error');
        return
    }
    if (!state.token) {
        openAuth();
        message($('authMessage'), 'Log in to search for available rooms.', 'info');
        return
    }
    const button = $('searchButton');
    button.disabled = true;
    button.textContent = 'Searching…';
    try {
        const qs = new URLSearchParams({
            check_in: ci,
            check_out: co,
            guest_count: '1'
        });
        const res = await api(`/api/rooms?${qs}`);
        if (!res.ok) {
            message($('searchMessage'), errorText(res.data, 'We couldn’t search rooms right now.'), 'error');
            return
        }
        renderRooms(Array.isArray(res.data.rooms) ? res.data.rooms : [])
    } catch (err) {
        message($('searchMessage'), err.message, 'error')
    } finally {
        button.disabled = false;
        button.innerHTML = 'Search rooms <span aria-hidden="true">→</span>'
    }
});
$('filterToggle').onclick = () => {
    $('filterPanel').hidden = !$('filterPanel').hidden
};
$('roomType').onchange = () => renderRooms(state.rooms);

function showBookingReview({
    roomType,
    checkIn,
    checkOut,
    phone,
    bookingId,
    status,
    priceTotalCents,
    currency = 'MYR',
    maxGuests,
    readOnly = false
}) {
    $('reviewEyebrow').textContent = readOnly ? 'Booking details' : 'Review your stay';
    $('reviewTitle').textContent = readOnly ? 'Your booking' : 'Confirm your booking';
    $('reviewSubtitle').textContent = readOnly ? 'These booking details are read-only.' : 'Check these details before confirming.';
    $('reviewRoom').textContent = roomTypeName(roomType);
    const profile=ROOM_PROFILES[roomType];
    $('reviewRoomDetails').hidden=!profile;
    if(profile){$('reviewRoomImage').src=profile.image;$('reviewRoomImage').alt=`${roomTypeName(roomType)} bedroom`; $('reviewBathroomImage').src=profile.bathroomImage;$('reviewBathroomImage').alt=`${roomTypeName(roomType)} bathroom`;$('reviewRoomGallery').scrollLeft=0;updateRoomGalleryCaption();$('reviewRoomHeading').textContent=roomTypeName(roomType);$('reviewRoomSummary').textContent=`${profile.bed} · ${profile.bathrooms} private bathroom${profile.bathrooms===1?'':'s'} with toilet · ${profile.area}${maxGuests?` · sleeps up to ${maxGuests}`:''}`;$('reviewAmenityList').replaceChildren(...profile.amenities.map(label=>{const item=document.createElement('span');item.textContent=label;return item}))}
    $('reviewDates').textContent = `${checkIn} → ${checkOut}`;
    $('reviewPhone').textContent = phone || 'Not provided';
    $('reviewPriceRow').hidden = priceTotalCents == null;
    $('reviewPrice').textContent = priceTotalCents == null ? '' : `${currency} ${(Number(priceTotalCents)/100).toFixed(2)}`;
    $('reviewBookingRow').hidden = !bookingId;
    $('reviewBooking').textContent = bookingId || '';
    $('reviewStatusRow').hidden = !status;
    $('reviewStatus').textContent = status || '';
    $('reviewDismiss').textContent = readOnly ? 'Close' : 'Go back';
    $('reviewConfirm').hidden = readOnly;
    $('guestNameRow').hidden=readOnly;
    $('reviewConfirm').disabled = false;
    $('reviewConfirm').textContent = 'Confirm booking';
    clearMessage($('reviewMessage'));
    $('bookingReviewOverlay').hidden = false
}

function closeBookingReview() {
    $('bookingReviewOverlay').hidden = true
}

function updateRoomGalleryCaption(){const gallery=$('reviewRoomGallery');const index=gallery.clientWidth?Math.round(gallery.scrollLeft/gallery.clientWidth):0;$('roomPhotoCaption').textContent=`${index===0?'Bedroom':'Bathroom'} · ${index+1} of 2`}
function scrollRoomGallery(direction){const gallery=$('reviewRoomGallery');gallery.scrollBy({left:direction*gallery.clientWidth,behavior:'smooth'})}

function chooseRoom(room) {
    if (!state.token) {
        openAuth();
        return
    }
    state.pending = {
        key: uuid(),
        body: {
            room_id: room.room_id,
            check_in: $('checkIn').value,
            check_out: $('checkOut').value,
            guest_count: 1,
            guest_name: ''
        },
        room
    };
    showBookingReview({
        roomType: room.room_type,
        checkIn: $('checkIn').value,
        checkOut: $('checkOut').value,
        phone: state.phoneNumber,
        priceTotalCents: Number(room.price_per_night_cents||0)*((new Date(`${$('checkOut').value}T00:00:00`)-new Date(`${$('checkIn').value}T00:00:00`))/86400000),
        currency: room.currency||'MYR',
        maxGuests: room.capacity
    })
}

function uuid() {
    return crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random().toString(16).slice(2)}-${Math.random().toString(16).slice(2)}`
}
async function submitBooking() {
    if (!state.pending) return;
    const guestName=$('guestName').value.trim();
    if(!guestName){ message($('reviewMessage'),'Enter the guest name before confirming.','error'); $('guestName').focus(); return }
    state.pending.body.guest_name=guestName;
    const button = $('reviewConfirm');
    button.disabled = true;
    button.textContent = 'Booking…';
    clearMessage($('reviewMessage'));
    try {
        const res = await api('/api/bookings', {
            method: 'POST',
            body: state.pending.body,
            key: state.pending.key
        });
        if (!res.ok) {
            message($('reviewMessage'), errorText(res.data, 'We couldn’t complete that booking.'), 'error');
            if (res.status < 500) {
                state.pending = null;
                button.hidden = true;
                $('reviewDismiss').textContent = 'Close'
            }
            return
        }
        const booking = res.data;
        if (booking.status === 'rejected') {
            message($('reviewMessage'), 'That room is no longer available. Close this window and search again.', 'error');
            state.pending = null;
            button.hidden = true;
            $('reviewDismiss').textContent = 'Close';
            return
        }
        const roomId = state.pending.room.room_id;
        state.pending = null;
        closeBookingReview();
        if (booking.booking_id) sessionStorage.setItem('airnstay:lastBookingId', booking.booking_id);
        state.rooms = state.rooms.filter(r => r.room_id !== roomId);
        renderRooms(state.rooms);
        showToast(booking.status === 'confirmed' ? 'Your stay is confirmed!' : 'Your booking is being confirmed…');
        loadBookings();
        loadNotifications();
        if (booking.status === 'pending' && booking.booking_id) watchBooking(booking.booking_id)
    } catch (err) {
        message($('reviewMessage'), `${err.message} You can retry this same booking safely.`, 'error')
    } finally {
        button.disabled = false;
        button.textContent = 'Confirm booking'
    }
}
async function loadBookings() {
    if (!state.token) return;
    try {
        const res = await api('/api/bookings');
        if (!res.ok) {
            $('bookingsEmpty').textContent = errorText(res.data, 'Could not load bookings.');
            return
        }
        renderBookings(res.data.bookings || [])
    } catch (err) {
        $('bookingsEmpty').textContent = err.message
    }
}

async function loadNotifications(){
    if(!state.token)return;
    try{const res=await api('/api/notifications');if(!res.ok)return;const items=res.data.notifications||[];const unread=Math.max(0,Number(res.data.unread_count)||0);const badge=$('notificationCount');badge.textContent=unread>99?'99+':String(unread);badge.hidden=unread===0;$('accountNotifications').setAttribute('aria-label',unread?`Open notifications, ${unread} unread`:'Open notifications');const list=$('notificationList');if(!list)return;list.replaceChildren();if(!items.length){list.innerHTML='<div class="empty">You have no notifications.</div>';return}for(const n of items){const el=document.createElement('article');el.className=`booking-item notification-item${n.is_read?'':' unread'}`;el.innerHTML=`<div class="booking-top"><strong>${escapeText(n.message)}</strong>${n.is_read?'':'<span class="notification-unread-dot" aria-label="Unread"></span>'}</div><div class="sub">${escapeText(n.booking_id)} · ${escapeText(new Date(n.created_at).toLocaleString())}</div>`;list.append(el)}}catch(e){}
}
async function markNotificationsRead(){if(!state.token)return;try{await api('/api/notifications/read',{method:'POST'});await loadNotifications()}catch(e){}}
async function clearNotifications(){if(!state.token)return;try{const res=await api('/api/notifications/clear',{method:'POST'});if(!res.ok)throw Error(errorText(res.data));await loadNotifications();showToast('Notifications cleared.')}catch(e){showToast(e.message)}}
async function loadAdminBookings(){if(!state.adminToken)return;try{const res=await api(`/admin/day?date=${encodeURIComponent($('adminDay').value||dateString(new Date()))}`);if(!res.ok)throw Error(errorText(res.data));const list=$('adminBookingList');list.replaceChildren();for(const b of res.data.bookings){const item=document.createElement('article');item.className='booking-item';const span=(new Date(b.check_out+'T00:00:00')-new Date(b.check_in+'T00:00:00'))/86400000>1;item.innerHTML=`<div class="booking-top"><div><strong>${escapeText(b.guest_name)} · Room ${escapeText(b.room_id)}</strong><div class="sub">${escapeText(b.phone_number)} · ${escapeText(b.booking_id)}</div></div><span class="badge ${escapeText(b.status)}">${escapeText(b.status)}</span></div><div class="booking-dates ${span?'multi-day':''}">${escapeText(b.check_in)} → ${escapeText(b.check_out)}</div><div class="booking-actions"><button data-checkin>Check in</button><button data-checkout>Check out</button><button data-modify>Change dates</button><button class="cancel" data-cancel>Cancel</button></div>`;for(const [sel,act] of [['[data-checkin]','checkin'],['[data-checkout]','checkout']])item.querySelector(sel).onclick=()=>adminAction(b,act);item.querySelector('[data-cancel]').onclick=()=>requestAdminCancellation(b);item.querySelector('[data-modify]').onclick=()=>modifyAdminBooking(b);list.append(item)}if(!res.data.bookings.length)list.innerHTML='<div class="empty">No active bookings for this day.</div>'}catch(e){message($('adminMessage'),e.message,'error')}}
async function loadAdminDashboardStats(){if(!state.adminToken)return;clearMessage($('adminStatsMessage'));try{const res=await api(`/admin/stats?date=${encodeURIComponent($('adminDay').value||dateString(new Date()))}`);if(!res.ok)throw Error(errorText(res.data));$('statArrivals').textContent=res.data.arrivals;$('statDepartures').textContent=res.data.departures;$('statInHouse').textContent=res.data.in_house;$('statCancelled').textContent=res.data.cancelled;$('statRevenue').textContent=`MYR ${(Number(res.data.arrival_revenue_cents||0)/100).toFixed(2)}`}catch(e){message($('adminStatsMessage'),e.message,'error')}}
async function loadAdminHistory(){if(!state.adminToken)return;clearMessage($('historyMessage'));const q=new URLSearchParams();if($('historyFrom').value)q.set('from_date',$('historyFrom').value);if($('historyTo').value)q.set('to_date',$('historyTo').value);if($('historyRoom').value)q.set('room_id',$('historyRoom').value);try{const res=await api(`/admin/bookings${q.size?'?'+q:''}`);if(!res.ok)throw Error(errorText(res.data));const list=$('historyBookingList');list.replaceChildren();const records=res.data.bookings||[];if(!records.length){list.innerHTML='<div class="empty">No booking records match these filters.</div>';return}for(const b of records){const item=document.createElement('article');item.className='booking-item';const multi=(new Date(b.check_out+'T00:00:00')-new Date(b.check_in+'T00:00:00'))/86400000>1;const total=b.total_price_cents==null?'—':`${escapeText(b.currency||'MYR')} ${(Number(b.total_price_cents)/100).toFixed(2)}`;item.innerHTML=`<div class="booking-top"><div><div class="booking-room">${escapeText(b.guest_name)} · Room ${escapeText(b.room_number||b.room_id)}</div><div class="booking-id">${escapeText(b.booking_id)}</div></div><span class="badge ${escapeText(b.status)}">${escapeText(b.status)}</span></div><div class="history-record-grid"><span>Phone</span><strong>${escapeText(b.phone_number)}</strong><span>Stay</span><strong class="booking-dates ${multi?'multi-day':''}">${escapeText(b.check_in)} → ${escapeText(b.check_out)}</strong><span>Total</span><strong>${total}</strong><span>Created</span><strong>${escapeText(new Date(b.created_at).toLocaleString())}</strong></div>`;list.append(item)}}catch(e){message($('historyMessage'),e.message,'error')}}
async function adminAction(b,action){try{const res=await api(`/admin/bookings/${encodeURIComponent(b.booking_id)}/${action}`,{method:'POST'});if(!res.ok)throw Error(errorText(res.data));showToast('Booking updated and user notified.');loadAdminBookings();loadAdminDashboardStats()}catch(e){message($('adminMessage'),e.message,'error')}}
function requestAdminCancellation(b){state.cancelTarget=null;state.adminCancelTarget=b;$('cancelTitle').textContent='Cancel this booking?';const room=b.room_number||b.room_id?.replace(/^R/,'')||'—';$('cancelCopy').innerHTML=`<div class="cancel-detail-row"><strong>Room</strong><span>${escapeText(room)}</span></div><div class="cancel-detail-row"><strong>Reference number</strong><span>${escapeText(b.booking_id)}</span></div><div class="cancel-detail-row"><strong>Stay dates</strong><span>${escapeText(b.check_in)} – ${escapeText(b.check_out)}</span></div><div class="cancel-detail-row"><strong>Cancellation</strong><span>This will cancel the guest’s booking and notify them.</span></div>`;clearMessage($('cancelMessage'));$('confirmCancel').textContent='Confirm cancellation';$('cancelOverlay').hidden=false}
function modifyAdminBooking(b){state.adminModifyTarget=b;$('modifyCheckIn').value=b.check_in;$('modifyCheckOut').value=b.check_out;const next=new Date(`${b.check_in}T12:00:00`);next.setDate(next.getDate()+1);$('modifyCheckOut').min=dateString(next);clearMessage($('adminDateMessage'));$('saveAdminDate').disabled=false;$('saveAdminDate').textContent='Save dates';$('adminDateOverlay').hidden=false}
async function saveAdminDateChange(e){e.preventDefault();const b=state.adminModifyTarget;if(!b)return;const ci=$('modifyCheckIn').value,co=$('modifyCheckOut').value;if(!ci||!co||co<=ci){message($('adminDateMessage'),'Choose a check-out date after check-in.','error');return}const button=$('saveAdminDate');button.disabled=true;button.textContent='Saving…';clearMessage($('adminDateMessage'));try{const res=await api(`/admin/bookings/${encodeURIComponent(b.booking_id)}/modify`,{method:'POST',body:{check_in:ci,check_out:co}});if(!res.ok)throw Error(errorText(res.data,'Could not change the booking dates.'));$('adminDateOverlay').hidden=true;state.adminModifyTarget=null;$('adminDay').value=ci;showToast('Dates updated and the guest was notified.');await loadAdminBookings();loadAdminDashboardStats()}catch(err){message($('adminDateMessage'),err.message,'error')}finally{button.disabled=false;button.textContent='Save dates'}}
function openAdminBooking(){state.adminAvailableRooms=[];state.adminSelectedRoom=null;$('adminBookingForm').reset();$('adminGuestCount').value='1';$('adminCheckIn').value=$('adminDay').value||dateString(new Date());const next=new Date(`${$('adminCheckIn').value}T12:00:00`);next.setDate(next.getDate()+1);$('adminCheckOut').value=dateString(next);$('adminRoomChoices').replaceChildren();$('adminBookingConfirm').disabled=true;clearMessage($('adminRoomMessage'));$('adminBookingOverlay').hidden=false}
async function searchAdminRooms(e){e.preventDefault();clearMessage($('adminRoomMessage'));$('adminRoomChoices').replaceChildren();$('adminBookingConfirm').disabled=true;state.adminSelectedRoom=null;const ci=$('adminCheckIn').value,co=$('adminCheckOut').value,guests=Number($('adminGuestCount').value);if(!ci||!co||co<=ci||guests<1||guests>8){message($('adminRoomMessage'),'Choose valid dates and a guest count from 1 to 8.','error');return}const button=$('adminSearchRooms');button.disabled=true;button.textContent='Searching…';try{const qs=new URLSearchParams({check_in:ci,check_out:co,guest_count:String(guests)});const res=await api(`/admin/rooms?${qs}`);if(!res.ok)throw Error(errorText(res.data));state.adminAvailableRooms=res.data.rooms||[];if(!state.adminAvailableRooms.length){message($('adminRoomMessage'),'No rooms are available for those dates. Try another date range.','info');return}const nights=(new Date(`${co}T00:00:00`)-new Date(`${ci}T00:00:00`))/86400000;for(const room of state.adminAvailableRooms){const card=document.createElement('article');card.className='room-card admin-room-card';const photo=document.createElement('img');photo.className='room-image';photo.src=ROOM_PROFILES[room.room_type]?.image||ROOM_PROFILES.single.image;photo.alt=roomTypeName(room.room_type);const copy=document.createElement('div');copy.className='room-copy';const nightly=Number(room.price_per_night_cents||0);copy.innerHTML=`<div class="room-name">${escapeText(roomTypeName(room.room_type))} · Room ${escapeText(room.room_number)}</div><div class="room-meta">Up to ${escapeText(room.capacity)} guests</div><div class="room-price">${escapeText(room.currency||'MYR')} ${(nightly/100).toFixed(2)} <small>/ night · ${escapeText(room.currency||'MYR')} ${(nightly*nights/100).toFixed(2)} total</small></div>`;const select=document.createElement('button');select.type='button';select.className='button secondary';select.textContent='Select room';select.onclick=()=>{state.adminSelectedRoom=room;document.querySelectorAll('.admin-room-card').forEach(el=>el.classList.remove('selected'));card.classList.add('selected');$('adminBookingConfirm').disabled=false};card.append(photo,copy,select);$('adminRoomChoices').append(card)}}catch(err){message($('adminRoomMessage'),err.message,'error')}finally{button.disabled=false;button.textContent='Search available rooms'}}
async function confirmAdminBooking(){const room=state.adminSelectedRoom;if(!room)return;const button=$('adminBookingConfirm');button.disabled=true;button.textContent='Booking…';try{const res=await api('/admin/bookings',{method:'POST',body:{phone_number:$('adminGuestPhone').value.trim(),guest_name:$('adminGuestName').value.trim(),room_id:room.room_id,check_in:$('adminCheckIn').value,check_out:$('adminCheckOut').value,guest_count:Number($('adminGuestCount').value)}});if(!res.ok)throw Error(errorText(res.data));$('adminBookingOverlay').hidden=true;$('adminDay').value=$('adminCheckIn').value;showToast('Booking confirmed. A notification was sent to the guest.');loadAdminBookings();loadAdminDashboardStats()}catch(err){message($('adminRoomMessage'),err.message,'error');button.disabled=false}finally{button.textContent='Confirm booking'}}

function renderBookings(bookings) {
    state.bookings = bookings;
    const list = $('bookingList');
    list.replaceChildren();
    $('bookingsEmpty').hidden = bookings.length > 0;
    if (!bookings.length) {
        $('bookingsEmpty').hidden = false;
        $('bookingsEmpty').textContent = 'No bookings yet. Search rooms to plan a stay.';
        return
    }
    for (const b of bookings) {
        const item = document.createElement('article');
        item.className = 'booking-item';
        const label = state.rooms.find(r => r.room_id === b.room_id);
        item.innerHTML = `<div class="booking-top"><div><div class="booking-room">${escapeText(label?roomLabel(label):`Room ${b.room_id}`)}</div><div class="booking-id">${escapeText(b.booking_id)}</div></div><span class="badge ${escapeText(b.status)}">${escapeText(b.status)}</span></div><div class="booking-dates">${escapeText(b.check_in)} → ${escapeText(b.check_out)}</div><div class="booking-actions"><button data-view>View details</button>${b.status==='confirmed'?'<button class="cancel" data-cancel>Cancel booking</button>':''}</div>`;
        item.querySelector('[data-view]').onclick = () => viewBooking(b.booking_id);
        const cancel = item.querySelector('[data-cancel]');
        if (cancel) cancel.onclick = () => cancelBooking(b.booking_id);
        list.append(item)
    }
}
async function viewBooking(id) {
    try {
        const res = await api(`/api/bookings/${encodeURIComponent(id)}`);
        if (!res.ok) {
            showToast(errorText(res.data, 'Could not load this booking.'));
            return
        }
        const b = res.data;
        const cached = state.rooms.find(room => room.room_id === b.room_id);
        showBookingReview({
            roomType: b.room_type || cached?.room_type,
            checkIn: b.check_in,
            checkOut: b.check_out,
            phone: state.phoneNumber,
            bookingId: b.booking_id,
            status: b.status,
            priceTotalCents: b.total_price_cents,
            currency: b.currency||'MYR',
            readOnly: true
        });
        if (['pending', 'cancelling'].includes(b.status)) watchBooking(id)
    } catch (err) {
        showToast(err.message)
    }
}

function cancelBooking(id) {
    const booking = state.bookings.find(item => item.booking_id === id);
    if (!booking) return;
    state.cancelTarget = booking;
    state.adminCancelTarget = null;
    $('cancelTitle').textContent = 'Are you sure?';
    $('confirmCancel').textContent = 'Cancel booking';
    const roomNumber = booking.room_number || booking.room_id?.replace(/^R/, '') || 'your room';
    $('cancelCopy').innerHTML = `<div class="cancel-detail-row"><strong>Room</strong><span>${escapeText(roomNumber)}</span></div><div class="cancel-detail-row"><strong>Reference number</strong><span>${escapeText(booking.booking_id)}</span></div><div class="cancel-detail-row"><strong>Stay dates</strong><span>${escapeText(booking.check_in)} – ${escapeText(booking.check_out)}</span></div><div class="cancel-detail-row"><strong>Cancellation</strong><span>The room will be removed from your booking.</span></div>`;
    clearMessage($('cancelMessage'));
    $('cancelOverlay').hidden = false
}
async function confirmCancellation() {
    const isAdmin = Boolean(state.adminCancelTarget);
    const target = isAdmin ? state.adminCancelTarget : state.cancelTarget;
    if (!target) return;
    const button = $('confirmCancel');
    button.disabled = true;
    button.textContent = 'Cancelling…';
    clearMessage($('cancelMessage'));
    try {
        const path = isAdmin ? `/admin/bookings/${encodeURIComponent(target.booking_id)}/cancel` : `/api/bookings/${encodeURIComponent(target.booking_id)}/cancel`;
        const res = await api(path, {
            method: 'POST'
        });
        if (!res.ok) {
            message($('cancelMessage'), errorText(res.data, 'Could not cancel this booking.'), 'error');
            return
        }
        const id = target.booking_id;
        closeCancellationOverlay();
        if (isAdmin) {
            showToast('Booking cancelled and the guest was notified.');
            await loadAdminBookings();
            loadAdminDashboardStats();
            return
        }
        if (res.data.status === 'cancelling') watchBooking(id);
        showToast(res.data.status === 'cancelled' ? 'Booking cancelled.' : 'Cancellation is processing…');
        loadBookings();
        loadNotifications()
    } catch (err) {
        message($('cancelMessage'), err.message, 'error')
    } finally {
        button.disabled = false;
        button.textContent = 'Cancel booking'
    }
}

function closeCancellationOverlay() {
    $('cancelOverlay').hidden = true;
    state.cancelTarget = null;
    state.adminCancelTarget = null;
    $('cancelTitle').textContent = 'Are you sure?';
    $('confirmCancel').textContent = 'Cancel booking';
}

function watchBooking(id) {
    if (state.pollers.has(id)) return;
    let attempts = 0;
    const timer = setInterval(async () => {
        attempts++;
        try {
            const res = await api(`/api/bookings/${encodeURIComponent(id)}`);
            if (res.ok && !['pending', 'cancelling'].includes(res.data.status)) {
                clearInterval(timer);
                state.pollers.delete(id);
                loadBookings();
                showToast(res.data.status === 'cancelled' ? 'Booking cancelled.' : res.data.status === 'confirmed' ? 'Your stay is confirmed!' : 'Booking status updated.');
                return
            }
        } catch (e) {}
        if (attempts >= 15) {
            clearInterval(timer);
            state.pollers.delete(id);
            loadBookings();
            showToast('Status check paused. Refresh your bookings to check again.')
        }
    }, 2000);
    state.pollers.set(id, timer)
}
$('authForm').addEventListener('submit', async e => {
    e.preventDefault();
    clearMessage($('authMessage'));
    const button = $('authSubmit');
    button.disabled = true;
    button.textContent = 'Opening session…';
    const phoneNumber = $('authIdentity').value.trim();
    try {
        if(state.authMode==='admin'){
            const result=await api('/api/admin/login',{method:'POST',auth:false,body:{number:phoneNumber,password:$('adminPassword').value}});
            if(!result.ok){message($('authMessage'),errorText(result.data),'error');return}state.adminToken=result.data.token;$('authOverlay').hidden=true;renderAccount();return
        }
        const identify=await api('/api/admin/identify',{method:'POST',auth:false,body:{number:phoneNumber}});
        if(identify.ok){setAuthMode('admin',phoneNumber);message($('authMessage'),'Admin sequence recognized. Enter your password to continue.','ok');return}
        const res = await api('/api/login', {
            method: 'POST',
            auth: false,
            body: {
                phone_number: phoneNumber
            }
        });
        if (!res.ok) {
            message($('authMessage'), errorText(res.data, 'Could not open your session.'), 'error');
            return
        }
        state.token = res.data.token;
        state.phoneNumber = phoneNumber;
        $('authOverlay').hidden = true;
        renderAccount();
        showToast('Welcome back. Your session is open.')
    } catch (err) {
        message($('authMessage'), err.message, 'error')
    } finally {
        button.disabled = false;
        button.textContent = state.authMode==='admin'?'Sign in':'Continue'
    }
});
async function signOut() {
    const token = state.token;
    let revoked = false;
    try {
        const response = await fetch(`${API_BASE}/api/logout`, {
            method: 'POST',
            headers: {
                'X-Customer-Token': token
            }
        });
        revoked = response.ok
    } catch (e) {}
    state.token = null;
    state.phoneNumber = null;
    renderAccount();
    showToast(revoked ? 'You’ve been signed out.' : 'Signed out on this device; the server session could not be revoked.')
}
$('accountButton').onclick = () => {
    if (state.adminToken) {fetch(`${API_BASE}/api/admin/logout`,{method:'POST',headers:{'X-Admin-Token':state.adminToken}});state.adminToken=null;renderAccount()}
    else if (state.token) signOut();
    else openAuth()
};
$('closeAuth').onclick = () => $('authOverlay').hidden = true;
$('refreshBookings').onclick = loadBookings;
document.querySelectorAll('[data-tab]').forEach(button => button.onclick = () => switchTab(button.dataset.tab.replace(/Tab$/,'')));
$('accountNotifications').onclick=()=>switchTab('notifications');
$('newAdminBooking').onclick=openAdminBooking;$('adminBookingForm').onsubmit=searchAdminRooms;$('adminBookingConfirm').onclick=confirmAdminBooking;$('closeAdminBooking').onclick=()=>$('adminBookingOverlay').hidden=true;$('adminBookingCancel').onclick=()=>$('adminBookingOverlay').hidden=true;
$('adminCheckIn').addEventListener('change',clearAdminRoomSelection);$('adminCheckOut').addEventListener('change',clearAdminRoomSelection);$('adminGuestCount').addEventListener('change',clearAdminRoomSelection);
$('clearNotifications').onclick=clearNotifications;
setInterval(()=>{if(state.token)loadNotifications()},5000);
$('adminDay').value=dateString(new Date());$('adminDay').onchange=()=>{loadAdminBookings();loadAdminDashboardStats()};
function setAuthMode(mode,identity=''){state.authMode=mode;const admin=mode==='admin';$('authTitle').textContent=admin?'Administrator sign in':'Good to have you here.';$('authSubtitle').textContent=admin?'Enter your admin password to continue.':'Enter your phone number to open your session.';$('authIdentityLabel').firstChild.textContent=admin?'Admin access sequence':'Phone number';$('authIdentity').type='text';$('authIdentity').inputMode='text';$('authIdentity').readOnly=admin;$('authIdentity').placeholder=admin?'Admin access sequence':'+60123456789';$('adminPasswordLabel').hidden=!admin;$('adminPassword').required=admin;$('authSubmit').textContent=admin?'Sign in':'Continue';$('authIdentity').value=identity;$('adminPassword').value=''}
function clearAdminRoomSelection(){state.adminAvailableRooms=[];state.adminSelectedRoom=null;$('adminRoomChoices').replaceChildren();$('adminBookingConfirm').disabled=true}
$('closeReview').onclick = closeBookingReview;
$('reviewDismiss').onclick = closeBookingReview;
$('reviewConfirm').onclick = submitBooking;
$('reviewImagePrev').onclick=()=>scrollRoomGallery(-1);
$('reviewImageNext').onclick=()=>scrollRoomGallery(1);
$('reviewRoomGallery').addEventListener('scroll',updateRoomGalleryCaption);
$('closeCancel').onclick = closeCancellationOverlay;
$('keepBooking').onclick = closeCancellationOverlay;
$('confirmCancel').onclick = confirmCancellation;
$('adminDateForm').onsubmit=saveAdminDateChange;
$('historyFilterForm').onsubmit=e=>{e.preventDefault();loadAdminHistory()};
$('clearHistoryFilters').onclick=()=>{$('historyFilterForm').reset();loadAdminHistory()};
$('closeAdminDate').onclick=()=>$('adminDateOverlay').hidden=true;
$('cancelAdminDate').onclick=()=>$('adminDateOverlay').hidden=true;
$('modifyCheckIn').addEventListener('change',()=>{const next=new Date(`${$('modifyCheckIn').value}T12:00:00`);next.setDate(next.getDate()+1);const min=dateString(next);$('modifyCheckOut').min=min;if($('modifyCheckOut').value<= $('modifyCheckIn').value)$('modifyCheckOut').value=min});
// Token intentionally remains in memory only; entering the same phone number reopens its booking identity.
